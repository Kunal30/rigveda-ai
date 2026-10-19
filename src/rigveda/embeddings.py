"""Meaning-based vectors for chunks, so searches match paraphrases ("monthly rent" ~ "per month for rent")."""

from __future__ import annotations

import sqlite3
import threading
from typing import Callable

import numpy

from . import llm
from .config import Config

BATCH = 32


def _normalized(vectors: list[list[float]]) -> numpy.ndarray:
    matrix = numpy.asarray(vectors, dtype=numpy.float32)
    norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / numpy.where(norms == 0, 1, norms)


def embed_missing(con: sqlite3.Connection, config: Config, log: Callable[[str], None]) -> int:
    """Embed every chunk that has no vector for the configured model. Returns how many were embedded."""
    rows = con.execute("""SELECT chunks.id, chunks.text FROM chunks
        LEFT JOIN embeddings ON embeddings.chunk_id=chunks.id AND embeddings.model=?
        WHERE embeddings.chunk_id IS NULL ORDER BY chunks.id""", (config.embedding_model,)).fetchall()
    done = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        try:
            vectors = _normalized(llm.embed(config.ollama, config.embedding_model, [row[1] for row in batch]))
        except llm.OllamaError as error:
            log(f"Meaning search unavailable for {len(rows) - done} chunk(s) until Ollama is reachable: {error}")
            break
        with con:
            con.executemany("INSERT OR REPLACE INTO embeddings(chunk_id, model, vector) VALUES (?, ?, ?)",
                            [(row[0], config.embedding_model, vector.tobytes()) for row, vector in zip(batch, vectors)])
        done += len(batch)
        if len(rows) > 500 and (start // BATCH) % 50 == 0:
            log(f"Embedding chunks for meaning search: {done}/{len(rows)}")
    return done


class _Vectors:
    """Every chunk's vector for one database and model, kept in memory as float16 (2 KB per chunk) so a question
    doesn't read them all from disk. New chunks are loaded as they appear; deleted ones are dropped by id."""

    def __init__(self) -> None:
        self.ids = numpy.empty(0, dtype=numpy.int64)
        self.matrix: numpy.ndarray | None = None
        self.lock = threading.Lock()

    def update(self, con: sqlite3.Connection, model: str) -> None:
        last = int(self.ids[-1]) if len(self.ids) else -1
        rows = con.execute("SELECT chunk_id, vector FROM embeddings WHERE model=? AND chunk_id>? ORDER BY chunk_id",
                           (model, last)).fetchall()
        if rows:
            added = numpy.frombuffer(b"".join(row[1] for row in rows), dtype=numpy.float32).reshape(len(rows), -1)
            ids = numpy.fromiter((row[0] for row in rows), dtype=numpy.int64, count=len(rows))
            self.matrix = added.astype(numpy.float16) if self.matrix is None else numpy.vstack([self.matrix, added.astype(numpy.float16)])
            self.ids = numpy.concatenate([self.ids, ids])
        count = con.execute("SELECT COUNT(*) FROM embeddings WHERE model=?", (model,)).fetchone()[0]
        if count != len(self.ids):  # some chunks were removed or re-embedded
            present = numpy.fromiter((row[0] for row in con.execute(
                "SELECT chunk_id FROM embeddings WHERE model=? ORDER BY chunk_id", (model,))), dtype=numpy.int64)
            keep = numpy.isin(self.ids, present)
            self.ids, self.matrix = self.ids[keep], self.matrix[keep]
            if len(self.ids) != count:  # a replaced vector below the last id: start over
                self.ids, self.matrix = numpy.empty(0, dtype=numpy.int64), None
                self.update(con, model)

    def best(self, query: numpy.ndarray, limit: int) -> list[int]:
        if self.matrix is None or not len(self.ids):
            return []
        scores = numpy.empty(len(self.ids), dtype=numpy.float32)
        for start in range(0, len(self.ids), 65536):  # in blocks, so float16 → float32 never doubles memory
            scores[start:start + 65536] = self.matrix[start:start + 65536].astype(numpy.float32) @ query
        best = numpy.argpartition(-scores, min(limit, len(scores) - 1))[:limit]
        return [int(self.ids[i]) for i in best[numpy.argsort(-scores[best])]]


_cache: dict[tuple[str, str], _Vectors] = {}


def nearest(con: sqlite3.Connection, config: Config, question: str, limit: int) -> list[int]:
    """Chunk ids most similar in meaning to the question, best first."""
    database = con.execute("PRAGMA database_list").fetchone()[2]
    vectors = _cache.setdefault((database, config.embedding_model), _Vectors())
    with vectors.lock:
        vectors.update(con, config.embedding_model)
        if not len(vectors.ids):
            return []
        query = _normalized(llm.embed(config.ollama, config.embedding_model, [question]))[0]
        return vectors.best(query, limit)

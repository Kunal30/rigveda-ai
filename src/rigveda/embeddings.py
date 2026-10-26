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
    """Every chunk's vector for one database and model, kept in memory as float32 (4 KB per chunk; 20 GB for 4.9
    million) so a question doesn't read them all from disk: against 4.9 million chunks a question takes 0.4 s, where
    float16 took 6 s, converted on every search (2026-10-25). New chunks are loaded as they appear, into room kept
    spare so the lot isn't copied each time; deleted ones are dropped by id."""

    BATCH = 20_000  # rows read from the database at a time, so loading never holds all of them twice

    def __init__(self) -> None:
        self.ids = numpy.empty(0, dtype=numpy.int64)
        self.buffer: numpy.ndarray | None = None  # rows beyond len(self.ids) are room to grow into
        self.lock = threading.Lock()

    @property
    def matrix(self) -> numpy.ndarray | None:
        return None if self.buffer is None else self.buffer[: len(self.ids)]

    def _append(self, ids: numpy.ndarray, rows: numpy.ndarray) -> None:
        size, needed = len(self.ids), len(self.ids) + len(rows)
        if self.buffer is None or needed > len(self.buffer):
            grown = numpy.empty((max(needed, int(needed * 1.1) + 1000), rows.shape[1]), dtype=numpy.float32)
            if size:
                grown[:size] = self.buffer[:size]
            self.buffer = grown
        self.buffer[size:needed] = rows
        self.ids = numpy.concatenate([self.ids, ids])

    def update(self, con: sqlite3.Connection, model: str) -> None:
        last = int(self.ids[-1]) if len(self.ids) else -1
        cursor = con.execute("SELECT chunk_id, vector FROM embeddings WHERE model=? AND chunk_id>? ORDER BY chunk_id",
                             (model, last))
        while rows := cursor.fetchmany(self.BATCH):
            added = numpy.frombuffer(b"".join(row[1] for row in rows), dtype=numpy.float32).reshape(len(rows), -1)
            self._append(numpy.fromiter((row[0] for row in rows), dtype=numpy.int64, count=len(rows)), added)
        count = con.execute("SELECT COUNT(*) FROM embeddings WHERE model=?", (model,)).fetchone()[0]
        if count != len(self.ids):  # some chunks were removed or re-embedded
            present = numpy.fromiter((row[0] for row in con.execute(
                "SELECT chunk_id FROM embeddings WHERE model=? ORDER BY chunk_id", (model,))), dtype=numpy.int64)
            keep = numpy.isin(self.ids, present)
            self.ids, self.buffer = self.ids[keep], self.matrix[keep]
            if len(self.ids) != count:  # a replaced vector below the last id: start over
                self.ids, self.buffer = numpy.empty(0, dtype=numpy.int64), None
                self.update(con, model)

    def best(self, query: numpy.ndarray, limit: int) -> list[int]:
        matrix = self.matrix
        if matrix is None or not len(matrix):
            return []
        scores = matrix @ query.astype(numpy.float32)
        best = numpy.argpartition(-scores, min(limit, len(scores) - 1))[:limit]
        return [int(self.ids[i]) for i in best[numpy.argsort(-scores[best])]]


_cache: dict[tuple[str, str], _Vectors] = {}


def nearest(con: sqlite3.Connection, config: Config, question: str, limit: int) -> list[int]:
    """Chunk ids most similar in meaning to the question, best first: from the server (`rigveda serve`), which keeps
    the vectors in memory, when `SERVER` is set and it's running; else from this process's own copy."""
    database = con.execute("PRAGMA database_list").fetchone()[2]
    if SERVER:
        try:
            return _from_server(question, limit, database)
        except OSError:  # not running: load them here instead
            pass
    vectors = _cache.setdefault((database, config.embedding_model), _Vectors())
    with vectors.lock:
        vectors.update(con, config.embedding_model)
        if not len(vectors.ids):
            return []
        query = _normalized(llm.embed(config.ollama, config.embedding_model, [question]))[0]
        return vectors.best(query, limit)


SERVER = ""  # e.g. "http://127.0.0.1:8765": the voice listener asks the server rather than hold 20 GB of its own
SERVER_WAIT = 300  # seconds; the server's first search loads every vector, which takes a while


def _from_server(question: str, limit: int, database: str) -> list[int]:
    import json
    from urllib.request import Request, urlopen

    request = Request(SERVER + "/api/nearest", data=json.dumps({"question": question, "limit": limit}).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=SERVER_WAIT) as response:
        reply = json.load(response)
    if reply.get("database") != database:  # a server over another index
        raise OSError("the server searches another database")
    return [int(chunk_id) for chunk_id in reply["ids"]]


def warm(database, config: Config) -> None:
    """Load the vectors now, so the first question isn't the one that waits."""
    from .store import connect

    con = connect(database)
    try:
        path = con.execute("PRAGMA database_list").fetchone()[2]
        vectors = _cache.setdefault((path, config.embedding_model), _Vectors())
        with vectors.lock:
            vectors.update(con, config.embedding_model)
    finally:
        con.close()

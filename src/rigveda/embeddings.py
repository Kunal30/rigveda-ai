"""Meaning-based vectors for chunks, so searches match paraphrases ("monthly rent" ~ "per month for rent")."""

from __future__ import annotations

import sqlite3
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


def nearest(con: sqlite3.Connection, config: Config, question: str, limit: int) -> list[int]:
    """Chunk ids most similar in meaning to the question, best first."""
    rows = con.execute("SELECT chunk_id, vector FROM embeddings WHERE model=?", (config.embedding_model,)).fetchall()
    if not rows:
        return []
    query = _normalized(llm.embed(config.ollama, config.embedding_model, [question]))[0]
    matrix = numpy.frombuffer(b"".join(row[1] for row in rows), dtype=numpy.float32).reshape(len(rows), -1)
    scores = matrix @ query
    best = numpy.argsort(-scores)[:limit]
    return [rows[i][0] for i in best]

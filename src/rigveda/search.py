"""Hybrid search over indexed chunks: exact words plus meaning."""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

from .config import Config
from .store import connect

CANDIDATES = 50  # results taken from each method before merging
# Reciprocal rank fusion. Tuned on lease questions with known answers: meaning search found
# paraphrased answers far better, so keywords count for half and top ranks weigh more (k=20, not 60).
RRF_K = 20
KEYWORD_WEIGHT = 0.5

# Question words that match nearly every chunk and drown out the terms that matter.
STOPWORDS = {
    "a", "about", "am", "an", "and", "any", "are", "as", "at", "be", "can", "did", "do", "does", "for", "from", "has",
    "have", "how", "i", "i'm", "in", "is", "it", "me", "my", "of", "on", "or", "our", "show", "tell", "that", "the",
    "there", "this", "to", "was", "what", "what's", "when", "where", "which", "who", "whom", "why", "will", "with",
    "you", "your",
}


def keyword_ids(con: sqlite3.Connection, question: str, limit: int) -> list[int]:
    terms = re.findall(r"[\w'-]+", question.lower())
    terms = [term for term in terms if term not in STOPWORDS] or terms
    if not terms:
        return []
    query = " OR ".join(f'"{term}"' for term in terms)
    rows = con.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?", (query, limit))
    return [row[0] for row in rows]


def search(db: Path, question: str, limit: int, config: Config | None = None) -> list[sqlite3.Row]:
    """Best chunks for a question. With a config, meaning search is merged with keyword search."""
    con = connect(db)
    try:
        rankings = [(KEYWORD_WEIGHT, keyword_ids(con, question, CANDIDATES))]
        if config is not None:
            from . import embeddings, llm

            try:
                rankings.append((1.0, embeddings.nearest(con, config, question, CANDIDATES)))
            except llm.OllamaError as error:
                print(f"Meaning search unavailable, using keywords only: {error}", file=sys.stderr)
        scores: dict[int, float] = {}
        for weight, ranking in rankings:
            for rank, chunk_id in enumerate(ranking):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (RRF_K + rank + 1)
        results, seen = [], set()
        for chunk_id in sorted(scores, key=scores.get, reverse=True):
            row = con.execute("SELECT id, path, ordinal, location, text FROM chunks WHERE id=?", (chunk_id,)).fetchone()
            key = " ".join(row["text"].split())
            if key in seen:  # the same text in another copy of a file
                continue
            seen.add(key)
            results.append(row)
            if len(results) == limit:
                break
        return results
    finally:
        con.close()


def cite(row) -> str:
    """Human-readable source, e.g. `talk.mp4 @ 12:30` or `deck.pptx (slide 4)`."""
    location, path = row["location"], row["path"]
    if path.startswith("gdrive://"):  # gdrive://<id>/<name>
        path = "Google Drive: " + path.split("/", 3)[-1]
    if not location:
        return f"{path} (chunk {row['ordinal'] + 1})"
    if location[0].isdigit():
        return f"{path} @ {location}"
    return f"{path} ({location})"

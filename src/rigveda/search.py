"""Full-text search over indexed chunks."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .store import connect


def search(db: Path, question: str, limit: int) -> list[sqlite3.Row]:
    terms = re.findall(r"[\w'-]+", question)
    if not terms:
        return []
    query = " OR ".join(f'"{term}"' for term in terms)
    con = connect(db)
    rows = con.execute("""SELECT chunks.path, chunks.ordinal, chunks.location, chunks.text, bm25(chunks_fts) score
        FROM chunks_fts JOIN chunks ON chunks.id=chunks_fts.rowid
        WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?""", (query, limit)).fetchall()
    con.close()
    return rows


def cite(row) -> str:
    """Human-readable source, e.g. `talk.mp4 @ 12:30` or `deck.pptx (slide 4)`."""
    location = row["location"]
    if not location:
        return f"{row['path']} (chunk {row['ordinal'] + 1})"
    if location[0].isdigit():
        return f"{row['path']} @ {location}"
    return f"{row['path']} ({location})"

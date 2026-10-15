"""SQLite storage for indexed documents and their text chunks."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (path TEXT PRIMARY KEY, modified_ns INTEGER NOT NULL, size INTEGER NOT NULL, indexed_at INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'text', version TEXT NOT NULL DEFAULT '1');
CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE, ordinal INTEGER NOT NULL, location TEXT NOT NULL DEFAULT '', text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text);
CREATE INDEX IF NOT EXISTS chunks_path_index ON chunks(path);
"""

# Columns added after v0.1; older databases are upgraded in place.
MIGRATIONS = {
    "documents": {"kind": "TEXT NOT NULL DEFAULT 'text'", "version": "TEXT NOT NULL DEFAULT '1'"},
    "chunks": {"location": "TEXT NOT NULL DEFAULT ''"},
}


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    os.chmod(path, 0o600)  # the index holds extracted text from private files
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    return con


def remove_document(con: sqlite3.Connection, path: str) -> None:
    for row in con.execute("SELECT id FROM chunks WHERE path=?", (path,)).fetchall():
        con.execute("DELETE FROM chunks_fts WHERE rowid=?", (row[0],))
    con.execute("DELETE FROM documents WHERE path=?", (path,))


def documents_under(con: sqlite3.Connection, root: Path) -> list[str]:
    """Indexed paths equal to `root` or inside it."""
    prefix = str(root).rstrip("/") + "/"
    rows = con.execute("SELECT path FROM documents WHERE path=? OR substr(path, 1, ?)=?", (str(root), len(prefix), prefix))
    return [row[0] for row in rows]

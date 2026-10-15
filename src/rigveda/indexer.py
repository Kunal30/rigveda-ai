"""Index files into the local database: extract, chunk, store, and prune deleted files."""

from __future__ import annotations

import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import extract
from .chunking import chunks
from .config import Config
from .store import connect, documents_under, remove_document

SKIP = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__"}
SLOW_KINDS = {"audio", "video", "image"}

Log = Callable[[str], None]


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


@dataclass
class IndexStats:
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    removed: int = 0

    def __str__(self) -> str:
        return f"{self.updated} updated, {self.unchanged} unchanged, {self.skipped} skipped, {self.removed} removed"


def ignored(path: Path, root: Path) -> bool:
    """Hidden, dependency, build and temporary files are not indexed."""
    try:
        parts = path.relative_to(root).parts if path != root else (path.name,)
    except ValueError:
        parts = path.parts
    name = path.name
    return any(part.startswith(".") or part in SKIP for part in parts) or name.startswith("~$")


def iter_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        if extract.supported(root):
            yield root
        return
    for path in root.rglob("*"):
        if not ignored(path, root) and path.is_file() and not path.is_symlink() and extract.supported(path):
            yield path


def index_file(con: sqlite3.Connection, path: Path, config: Config, log: Log = _stderr) -> str:
    """Index one file. Returns "updated", "unchanged" or "skipped"."""
    try:
        stat = path.stat()
        known = con.execute("SELECT modified_ns, size FROM documents WHERE path=?", (str(path),)).fetchone()
        if known and tuple(known) == (stat.st_mtime_ns, stat.st_size):
            return "unchanged"
        kind = extract.kind(path)
        if kind in SLOW_KINDS:
            log(f"Reading {kind}: {path}")
        segments = extract.extract(path, config)
    except extract.ExtractionError as error:
        log(f"Skipping {path}: {error}")
        return "skipped"
    except Exception as error:  # corrupt or unreadable file; keep going with the rest
        log(f"Skipping {path}: {type(error).__name__}: {error}")
        return "skipped"
    with con:
        remove_document(con, str(path))
        con.execute("INSERT INTO documents(path, modified_ns, size, indexed_at, kind) VALUES (?, ?, ?, ?, ?)",
                    (str(path), stat.st_mtime_ns, stat.st_size, int(time.time()), kind))
        ordinal = 0
        for segment in segments:
            for chunk in chunks(segment.text):
                cursor = con.execute("INSERT INTO chunks(path, ordinal, location, text) VALUES (?, ?, ?, ?)",
                                     (str(path), ordinal, segment.location, chunk))
                con.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)", (cursor.lastrowid, chunk))
                ordinal += 1
    return "updated"


def remove_path(con: sqlite3.Connection, path: Path) -> int:
    """Remove a file, or everything under a folder, from the index."""
    paths = documents_under(con, path)
    with con:
        for item in paths:
            remove_document(con, item)
    return len(paths)


def index_paths(db: Path, roots: list[str], config: Config, log: Log = _stderr) -> IndexStats:
    stats, con = IndexStats(), connect(db)
    try:
        for raw_root in roots:
            root = Path(raw_root).expanduser().resolve()
            if not root.exists():
                log(f"Not found: {root}")
                stats.skipped += 1
                continue
            seen = set()
            for path in iter_files(root):
                seen.add(str(path))
                status = index_file(con, path, config, log)
                setattr(stats, status, getattr(stats, status) + 1)
            # Forget files that were deleted, moved, or are now ignored since the last index.
            with con:
                for item in documents_under(con, root):
                    if item not in seen:
                        remove_document(con, item)
                        stats.removed += 1
    finally:
        con.close()
    return stats

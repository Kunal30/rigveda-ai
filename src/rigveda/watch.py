"""Keep the index up to date while files in the watched folders change."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from . import extract
from .config import Config
from .embeddings import embed_missing
from .indexer import Log, _stderr, ignored, index_file, index_paths, remove_path
from .store import connect

DEBOUNCE_SECONDS = 3  # wait for copies and saves to finish before reading a file


def root_for(path: Path, roots: list[Path]) -> Path | None:
    for root in roots:
        if path == root or root in path.parents:
            return root
    return None


def process(db: Path, paths: set[Path], roots: list[Path], config: Config, log: Log = _stderr) -> None:
    """Re-index changed files and drop deleted ones."""
    con = connect(db)
    try:
        for path in sorted(paths):
            root = root_for(path, roots)
            if root is None or ignored(path, root):
                continue
            if path.is_dir():  # a folder created or moved in
                log(f"Indexed {path}: {index_paths(db, [str(path)], config, log)}")
            elif path.is_file() and extract.supported(path):
                if index_file(con, path, config, log) == "updated":
                    log(f"Indexed {path}")
            elif not path.exists() and remove_path(con, path):
                log(f"Removed {path}")
        embed_missing(con, config, log)
    finally:
        con.close()


def watch(db: Path, folders: list[str], config: Config, log: Log = _stderr) -> None:
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError as error:
        raise SystemExit("Install watchdog to watch folders: pip install -e '.[watch]'") from error

    roots = [Path(folder).expanduser().resolve() for folder in folders]
    log(f"Initial index: {index_paths(db, [str(root) for root in roots], config, log)}")

    pending: set[Path] = set()
    lock, last_event = threading.Lock(), [0.0]

    class Collector(FileSystemEventHandler):
        def on_any_event(self, event) -> None:
            # A folder's own "modified" event fires for every change inside it; the file events cover those.
            if event.event_type in {"opened", "closed_no_write"} or (event.is_directory and event.event_type == "modified"):
                return
            with lock:
                pending.add(Path(event.src_path))
                if getattr(event, "dest_path", ""):
                    pending.add(Path(event.dest_path))
                last_event[0] = time.monotonic()

    observer = Observer()
    for root in roots:
        observer.schedule(Collector(), str(root), recursive=True)
    observer.start()
    log(f"Watching {len(roots)} folder(s). Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
            with lock:
                if not pending or time.monotonic() - last_event[0] < DEBOUNCE_SECONDS:
                    continue
                batch = set(pending)
                pending.clear()
            process(db, batch, roots, config, log)
    except KeyboardInterrupt:
        log("Stopped watching.")
    finally:
        observer.stop()
        observer.join()

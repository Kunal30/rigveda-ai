"""Index Google Drive in place: each file is downloaded to a private temporary folder, read, and deleted at once.

Only the text and a link back to Drive are kept, as a document at gdrive://<id>/<name>. The drive_files table
remembers every file's progress, so a run can stop at any point and the next one carries on. Files are read in
order of usefulness: documents and emails first, then archives and build output, then recordings, then images.
Several downloads run at once while one file at a time is read, so the GPU and the network are both kept busy.
"""

from __future__ import annotations

import datetime
import os
import shutil
import signal
import sqlite3
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import extract, gdrive
from .config import Config
from .embeddings import embed_missing
from .indexer import save
from .store import remove_document

Log = Callable[[str], None]

GOOGLE = gdrive.GOOGLE
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
EXPORTS = {  # Google's own formats, in the order to try; plain text when the Office export is over Google's 10 MB cap
    GOOGLE + "document": [(DOCX, ".docx"), ("text/plain", ".txt")],
    GOOGLE + "spreadsheet": [(XLSX, ".xlsx"), ("text/csv", ".csv")],
    GOOGLE + "presentation": [(PPTX, ".pptx"), ("text/plain", ".txt")],
    GOOGLE + "drawing": [("application/pdf", ".pdf")],
}
MAIL = {"message/rfc822", "message/news"}
STAGES = {  # survey group: when it's read
    "Google Docs, Sheets & Slides": 0, "PDFs": 0, "Word, Excel & PowerPoint": 0, "Text & data": 0,
    "Older Office files (.doc, .xls, .ppt)": 1, "Archives": 1, "Other": 1, "Other Google items (Forms, Drawings…)": 1,
    "Audio": 2, "Video": 2, "Images": 3,
}
STAGE_NAMES = ["documents and emails", "archives and other files", "audio and video", "images"]
WORKERS = 6
MAX_IN_FLIGHT = 3 * 2**30  # bytes being downloaded ahead of the reader; big videos wait their turn
EMBED_EVERY = 100


def cache_dir() -> Path:
    folder = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "rigveda" / "drive"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    return folder


def document_path(file_id: str, name: str) -> str:
    return f"gdrive://{file_id}/{name}"


def stage(mime: str, name: str) -> int:
    if mime in MAIL or name.lower().endswith((".eml", ".srt", ".vtt")):
        return 0
    return STAGES.get(gdrive.category(mime) or "Other", 1)


def _nanoseconds(stamp: str) -> int:
    moment = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return int(moment.timestamp() * 1_000_000_000)


def refresh(con: sqlite3.Connection, log: Log) -> dict:
    """Bring drive_files up to date with Drive: new and changed files become pending, removed ones leave the index."""
    known = {row["id"]: (row["name"], row["modified"], row["size"]) for row in
             con.execute("SELECT id, name, modified, size FROM drive_files")}
    seen: set[str] = set()
    added = changed = 0
    batch: list[tuple] = []

    def flush() -> None:
        with con:
            con.executemany("""INSERT INTO drive_files(id, name, mime, size, modified, link) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, mime=excluded.mime, size=excluded.size,
                modified=excluded.modified, link=excluded.link, state='pending', note='', attempts=0""", batch)
        batch.clear()

    for item in gdrive.files(lambda count: log(f"Listing Drive: {count:,} items") if count % 20000 == 0 else None,
                             fields="id, name, mimeType, size, modifiedTime, webViewLink"):
        if gdrive.category(item["mimeType"]) is None:  # folders
            continue
        seen.add(item["id"])
        row = (item["name"], item["modifiedTime"], int(item.get("size") or 0))
        if known.get(item["id"]) == row:
            continue
        if item["id"] in known:
            changed += 1
            old_name = known[item["id"]][0]
            if old_name != item["name"]:
                with con:
                    remove_document(con, document_path(item["id"], old_name))
        else:
            added += 1
        batch.append((item["id"], *row[:1], item["mimeType"], row[2], row[1], item.get("webViewLink", "")))
        if len(batch) >= 1000:
            flush()
    flush()
    removed = [file_id for file_id in known if file_id not in seen]
    with con:
        for file_id in removed:
            remove_document(con, document_path(file_id, known[file_id][0]))
            con.execute("DELETE FROM drive_files WHERE id=?", (file_id,))
    return {"files": len(seen), "added": added, "changed": changed, "removed": len(removed)}


def _suffix(name: str, mime: str) -> str:
    lower = name.lower()
    if lower.endswith(".tar.gz"):
        return ".tar.gz"
    if mime in MAIL and not lower.endswith(".eml"):
        return ".eml"
    suffix = Path(lower).suffix
    return suffix if 1 < len(suffix) <= 12 and suffix[1:].replace("_", "").isalnum() else ""


def fetch(file: sqlite3.Row, folder: Path) -> Path | None:
    """Download (or export) one file into `folder`. None for Google items that can't be exported, like Forms."""
    if file["mime"].startswith(GOOGLE):
        for export_mime, suffix in EXPORTS.get(file["mime"], []):
            target = folder / f"{file['id']}{suffix}"
            try:
                gdrive.download(f"files/{file['id']}/export", {"mimeType": export_mime}, target)
                return target
            except gdrive.ExportTooLarge:
                target.unlink(missing_ok=True)
        if file["mime"] in EXPORTS:
            raise gdrive.DriveError("too large for Google to export")
        return None
    target = folder / f"{file['id']}{_suffix(file['name'], file['mime'])}"
    try:
        gdrive.download(f"files/{file['id']}", {"alt": "media"}, target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _finish(con: sqlite3.Connection, file_id: str, state: str, note: str = "") -> None:
    with con:
        con.execute("UPDATE drive_files SET state=?, note=?, attempts=attempts+1, done_at=? WHERE id=?",
                    (state, note[:300], int(time.time()), file_id))


def read(con: sqlite3.Connection, file: sqlite3.Row, downloaded: Future, config: Config) -> str:
    """Read one downloaded file into the index and delete the download. Returns its new state."""
    try:
        path = downloaded.result()
    except gdrive.DriveError as error:
        _finish(con, file["id"], "failed", f"download: {error}")
        return "failed"
    if path is None:
        _finish(con, file["id"], "skipped", "Google can't export this kind of item")
        return "skipped"
    try:
        kind, segments = extract.extract_any(path, config)
        version = extract.version(path) if extract.supported(path) else "1"
    except extract.ExtractionError as error:  # a missing tool or model: try again on a later run
        _finish(con, file["id"], "waiting", str(error))
        return "waiting"
    except Exception as error:  # a damaged file
        _finish(con, file["id"], "failed", f"{type(error).__name__}: {error}")
        return "failed"
    finally:
        path.unlink(missing_ok=True)
    save(con, document_path(file["id"], file["name"]), kind, version, _nanoseconds(file["modified"]), file["size"], segments)
    state = "indexed" if segments else "empty"
    _finish(con, file["id"], state)
    return state


def _stop(signum, frame) -> None:
    """`systemctl stop` sends SIGTERM, which would skip clean-up; end like Ctrl+C instead, deleting downloads."""
    raise SystemExit(143)


def run(con: sqlite3.Connection, config: Config, log: Log, stages: set[int] | None = None, limit: int | None = None) -> dict:
    """Index every pending Drive file (or those in `stages`), in order of usefulness. Returns counts by outcome."""
    rows = con.execute("SELECT * FROM drive_files WHERE state IN ('pending', 'waiting')").fetchall()
    queue = sorted((row for row in rows if stages is None or stage(row["mime"], row["name"]) in stages),
                   key=lambda row: (stage(row["mime"], row["name"]), row["size"]))[:limit]
    counts: dict[str, int] = {}
    blank = [row for row in queue if not row["size"] and not row["mime"].startswith(GOOGLE)]  # nothing to download
    with con:
        con.executemany("UPDATE drive_files SET state='empty', note='0 bytes', attempts=attempts+1, done_at=? WHERE id=?",
                        [(int(time.time()), row["id"]) for row in blank])
    if blank:
        counts["empty"] = len(blank)
        queue = [row for row in queue if row["size"] or row["mime"].startswith(GOOGLE)]
    if not queue:
        return counts
    for stale in cache_dir().glob("run-*"):  # left by a run that was killed outright
        shutil.rmtree(stale, ignore_errors=True)
    folder = cache_dir() / f"run-{os.getpid()}"
    folder.mkdir(mode=0o700)
    umask = os.umask(0o077)  # downloads readable only by you, even inside the private folder
    previous = signal.signal(signal.SIGTERM, _stop)
    pool = ThreadPoolExecutor(WORKERS)
    pending, flight, done, started = deque(queue), deque(), 0, time.monotonic()
    current_stage = None

    def top_up() -> None:
        while pending and len(flight) < WORKERS * 3:
            in_flight = sum(file["size"] for file, _ in flight)
            if flight and in_flight + pending[0]["size"] > MAX_IN_FLIGHT:
                return
            file = pending.popleft()
            flight.append((file, pool.submit(fetch, file, folder)))

    try:
        top_up()
        while flight:
            file, downloaded = flight.popleft()
            top_up()
            now_stage = stage(file["mime"], file["name"])
            if now_stage != current_stage:
                current_stage = now_stage
                log(f"Drive: reading {STAGE_NAMES[now_stage]}")
            state = read(con, file, downloaded, config)
            counts[state] = counts.get(state, 0) + 1
            done += 1
            if done % EMBED_EVERY == 0:
                embed_missing(con, config, log)
                rate = done / (time.monotonic() - started)
                log(f"Drive: {done:,}/{len(queue):,} files read ({rate:.1f}/s) · "
                    + ", ".join(f"{count:,} {name}" for name, count in sorted(counts.items())))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # stopping doesn't wait for queued downloads
        signal.signal(signal.SIGTERM, previous)
        os.umask(umask)
        shutil.rmtree(folder, ignore_errors=True)
    embed_missing(con, config, log)
    return counts


def progress(con: sqlite3.Connection) -> dict:
    """Files and bytes per state, overall and per stage."""
    rows = con.execute("SELECT mime, name, state, size FROM drive_files").fetchall()
    by_state: dict[str, int] = {}
    by_stage = [{"name": name, "files": 0, "done": 0} for name in STAGE_NAMES]
    for row in rows:
        by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        entry = by_stage[stage(row["mime"], row["name"])]
        entry["files"] += 1
        entry["done"] += row["state"] not in ("pending", "waiting")
    return {"files": len(rows), "states": by_state, "stages": by_stage}

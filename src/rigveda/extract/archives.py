"""Archives (zip, jar, apk, tar, gz, ova): every file inside is read by the reader for its type.

Limits keep a hostile or enormous archive in check: members are counted and sized before they're unpacked, and
archives inside archives are opened only two levels deep. Libraries bundled into Java builds (BOOT-INF/lib,
WEB-INF/lib) are skipped: a Spring Boot jar carries the whole AWS SDK, hundreds of thousands of chunks that aren't
yours.
"""

from __future__ import annotations

import gzip
import shutil
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path

from . import Segment

MAX_MEMBERS = 5000
MAX_MEMBER = 512 * 2**20
MAX_TOTAL = 4 * 2**30
MAX_DEPTH = 2
ZIPS = (".zip", ".jar", ".apk", ".aar", ".war")
TARS = (".tar", ".tgz", ".ova")
BUNDLED = ("BOOT-INF/lib/", "WEB-INF/lib/")

_depth = threading.local()


def _read_member(stream, name: str, folder: Path, number: int, config) -> list[Segment]:
    from . import extract_any

    saved = folder / f"{number}-{Path(name).name or 'file'}"
    with saved.open("wb") as out:
        shutil.copyfileobj(stream, out, 2**20)
    try:
        _, found = extract_any(saved, config)
    except Exception:  # one unreadable member doesn't lose the rest
        return []
    finally:
        saved.unlink(missing_ok=True)
    return [Segment(segment.text, name + (f", {segment.location}" if segment.location else "")) for segment in found]


def bundled(name: str) -> bool:
    """A library packed into a Java build rather than a file of the user's own."""
    return any(folder in name for folder in BUNDLED)


def read_archive(path: Path, config) -> list[Segment]:
    level = getattr(_depth, "level", 0)
    if level >= MAX_DEPTH:
        return []
    _depth.level = level + 1
    try:
        with tempfile.TemporaryDirectory(prefix="rigveda-archive-") as folder:
            return _unpack(path, Path(folder), config)
    finally:
        _depth.level = level


def _unpack(path: Path, folder: Path, config) -> list[Segment]:
    name = path.name.lower()
    segments: list[Segment] = []
    total = 0
    if name.endswith(ZIPS):
        with zipfile.ZipFile(path) as archive:
            for number, info in enumerate(archive.infolist()[:MAX_MEMBERS]):
                if info.is_dir() or info.file_size > MAX_MEMBER or total + info.file_size > MAX_TOTAL:
                    continue
                if bundled(info.filename):
                    continue
                total += info.file_size
                with archive.open(info) as stream:
                    segments += _read_member(stream, info.filename, folder, number, config)
        return segments
    if name.endswith(TARS) or name.endswith(".tar.gz"):
        with tarfile.open(path, "r:*") as archive:
            for number, member in enumerate(archive):
                if number >= MAX_MEMBERS:
                    break
                if not member.isfile() or member.size > MAX_MEMBER or total + member.size > MAX_TOTAL:
                    continue
                if bundled(member.name):
                    continue
                total += member.size
                stream = archive.extractfile(member)
                if stream:
                    segments += _read_member(stream, member.name, folder, number, config)
        return segments
    # A single gzipped file: notes.txt.gz holds notes.txt.
    inner = path.name[:-3] if name.endswith(".gz") else path.name
    with gzip.open(path) as stream:
        limited = _Limited(stream, MAX_MEMBER)
        return _read_member(limited, inner, folder, 0, config)


class _Limited:
    """A read-only stream that stops after `limit` bytes, so a gzip bomb can't fill the disk."""

    def __init__(self, stream, limit: int) -> None:
        self.stream, self.left = stream, limit

    def read(self, size: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        data = self.stream.read(self.left if size < 0 else min(size, self.left))
        self.left -= len(data)
        return data


EXTRACTORS = [(suffix, "archive", read_archive) for suffix in (*ZIPS, *TARS, ".gz")]

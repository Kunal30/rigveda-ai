"""Files of no known type, such as build output or files without an extension: whatever text is in them.

Text files are read as text. Git's compressed objects are unpacked first. Anything else gives up its readable
runs of characters, the way the `strings` tool does, capped so a large binary can't flood the index.
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path

from . import Segment

MAX_READ = 16 * 2**20
MAX_CHARS = 100_000
RUNS = re.compile(rb"[\x20-\x7e\t]{6,}")


def looks_like_text(data: bytes) -> bool:
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError as error:
        return error.start >= len(sample) - 4  # only a character cut off at the end of the sample


def _git_object(data: bytes) -> bytes | None:
    if data[:1] != b"\x78":
        return None
    try:
        inflated = zlib.decompress(data)
    except zlib.error:
        return None
    header, _, body = inflated.partition(b"\x00")
    return body if header.split(b" ")[0] in (b"blob", b"commit", b"tag", b"tree") else None


def read_any(path: Path, config) -> list[Segment]:
    with path.open("rb") as file:
        data = file.read(MAX_READ)
    data = _git_object(data) or data
    if looks_like_text(data):
        text = data.decode("utf-8", "replace")
    else:
        text = "\n".join(run.decode("ascii") for run in RUNS.findall(data))
    text = text[:MAX_CHARS]
    return [Segment(text)] if text.strip() else []

"""Turn supported files into text segments that remember where they came from."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import Config


@dataclass(frozen=True)
class Segment:
    text: str
    location: str = ""  # "page 3", "slide 4", "sheet Budget", "12:30"


class ExtractionError(Exception):
    """A file can't be read right now (missing package, tool or model); retry on the next index."""


Extractor = Callable[[Path, Config], list[Segment]]


def _registry() -> dict[str, tuple[str, Extractor, str]]:
    from . import documents, images, media, text

    table: dict[str, tuple[str, Extractor, str]] = {}
    for module in (text, documents, images, media):
        for suffix, kind, extractor in module.EXTRACTORS:
            table[suffix] = (kind, extractor, getattr(module, "VERSIONS", {}).get(kind, "1"))
    return table


REGISTRY = _registry()


def supported(path: Path) -> bool:
    return path.suffix.lower() in REGISTRY


def kind(path: Path) -> str:
    return REGISTRY[path.suffix.lower()][0]


def version(path: Path) -> str:
    """Bumped when a kind's extraction improves, so already-indexed files are read again."""
    return REGISTRY[path.suffix.lower()][2]


def extract(path: Path, config: Config) -> list[Segment]:
    segments = REGISTRY[path.suffix.lower()][1](path, config)
    return [segment for segment in segments if segment.text.strip()]

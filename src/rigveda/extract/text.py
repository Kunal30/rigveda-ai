"""Plain text, Markdown, data and source-code files."""

from __future__ import annotations

from pathlib import Path

from . import Segment

SUFFIXES = {".c", ".cc", ".cpp", ".css", ".csv", ".go", ".h", ".html", ".java", ".js", ".json", ".md", ".py", ".rb", ".rs", ".rst", ".sh", ".sql", ".toml", ".ts", ".txt", ".xml", ".yaml", ".yml"}


def read_text(path: Path, config) -> list[Segment]:
    return [Segment(path.read_text(encoding="utf-8", errors="replace"))]


EXTRACTORS = [(suffix, "text", read_text) for suffix in sorted(SUFFIXES)]

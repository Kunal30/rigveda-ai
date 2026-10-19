"""Plain text, Markdown, data and source-code files."""

from __future__ import annotations

from pathlib import Path

from . import Segment

SUFFIXES = {".c", ".cc", ".cpp", ".css", ".csv", ".go", ".h", ".html", ".java", ".js", ".json", ".md", ".py", ".rb", ".rs", ".rst", ".sh", ".sql", ".toml", ".ts", ".txt", ".xml", ".yaml", ".yml"}


def read_text(path: Path, config) -> list[Segment]:
    return [Segment(path.read_text(encoding="utf-8", errors="replace"))]


def read_subtitles(path: Path, config) -> list[Segment]:
    """What's said in a subtitle file, without its cue numbers and timings."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    spoken = [line.strip() for line in lines if line.strip() and not line.strip().isdigit()
              and "-->" not in line and line.strip() != "WEBVTT"]
    return [Segment("\n".join(spoken))]


EXTRACTORS = [(suffix, "text", read_text) for suffix in sorted(SUFFIXES)] + [
    (".srt", "subtitles", read_subtitles), (".vtt", "subtitles", read_subtitles)]

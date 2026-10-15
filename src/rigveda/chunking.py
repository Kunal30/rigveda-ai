"""Split extracted text into overlapping chunks for search."""

from __future__ import annotations

import re


def chunks(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    result, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            breakpoint = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            end = breakpoint if breakpoint > start + size // 2 else end
        result.append(text[start:end].strip())
        start = end if end == len(text) else max(end - overlap, start + 1)
    return [item for item in result if item]

"""Rebuild a PDF page's text in visual reading order.

PDFs store text in whatever order the producing program wrote it. Filled-in
forms are the worst case: the blank template comes first and the typed-in
values are appended at the end of the page, far from their labels
("$ ____ per month for rent" ... "2605.00"). Reading words by their
position instead puts each value back on the line where it appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    line: tuple[int, int] = (0, 0)  # PyMuPDF's (block, line) numbers: storage order, not position

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float


# Control characters and private-use glyphs (e.g. empty checkbox symbols) carry no text.
def _meaningful(text: str) -> bool:
    return any(ch.isprintable() and not ("\ue000" <= ch <= "\uf8ff") and not ch.isspace() for ch in text)


def page_text(page) -> str:
    """Text of a PyMuPDF page in reading order: columns left to right, lines top to bottom."""
    words = _words(page)
    if not words:
        return ""
    return "\n".join(layout(words))


def _words(page) -> list[Word]:
    """Words with boxes derived from each character's baseline and font size.

    Font bounding boxes can't be trusted for line detection: some PDFs report a
    50-point-tall box for 10-point text. Baselines and sizes are reliable.
    Words are built character by character because PDFs often split a word
    across several fragments ("R" + "eturn") or omit space characters.
    """
    words: list[Word] = []
    for block_no, block in enumerate(page.get_text("rawdict")["blocks"]):
        for line_no, line in enumerate(block.get("lines", [])):
            current: list[tuple[dict, float, float]] = []  # (char, baseline, size)

            def flush() -> None:
                text = "".join(char["c"] for char, _, _ in current)
                if current and _meaningful(text):
                    first, baseline, size = current[0]
                    words.append(Word(first["bbox"][0], baseline - 0.8 * size, current[-1][0]["bbox"][2],
                                      baseline + 0.2 * size, text, (block_no, line_no)))
                current.clear()

            for span in line["spans"]:
                baseline, size = span["origin"][1], span["size"]
                for char in span["chars"]:
                    if char["c"].isspace():
                        flush()
                        continue
                    if current:
                        previous, previous_baseline, previous_size = current[-1]
                        gap = char["bbox"][0] - previous["bbox"][2]
                        if gap > 0.25 * max(size, previous_size) or abs(baseline - previous_baseline) > 0.5 * size:
                            flush()
                    current.append((char, baseline, size))
            flush()
    return words


def layout(words: list[Word], depth: int = 0) -> list[str]:
    stored = _stored_lines(words)
    gutter = _gutter(stored) if depth < 2 else None
    if gutter is None:
        return [_join(row) for row in _rows(words)]
    # Lines that cross the gutter (titles, full-width headings) separate bands of two-column text.
    breaks = sorted(box.y0 for box in stored if box.x0 < gutter < box.x1)
    bands: dict[int, list[Word]] = {}
    for word in words:
        band = sum(1 for y in breaks if y <= word.yc)
        side = 1 if word.x0 >= gutter else 0 if word.x1 <= gutter else 2  # 2 = full width
        bands.setdefault(band * 3 + side, []).append(word)
    lines: list[str] = []
    for key in sorted(bands):
        band = bands[key]
        lines.extend(layout(band, depth + 1) if key % 3 < 2 else [_join(row) for row in _rows(band)])
    return lines


def _stored_lines(words: list[Word]) -> list[Box]:
    """Bounding boxes of the lines as the PDF stores them."""
    groups: dict[tuple[int, int], list[Word]] = {}
    for word in words:
        groups.setdefault(word.line, []).append(word)
    return [Box(min(w.x0 for w in g), min(w.y0 for w in g), max(w.x1 for w in g), max(w.y1 for w in g)) for g in groups.values()]


def _rows(words: list[Word]) -> list[list[Word]]:
    """Group words whose vertical centres line up into visual lines (within one column)."""
    tolerance = 0.45 * median(w.y1 - w.y0 for w in words)
    rows: list[list[Word]] = []
    centre = 0.0
    for word in sorted(words, key=lambda w: (w.yc, w.x0)):
        if rows and abs(word.yc - centre) <= tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
            centre = word.yc  # compare against the row's first word so rows can't drift downwards
    return [sorted(row, key=lambda w: w.x0) for row in rows]


def _join(row: list[Word]) -> str:
    return " ".join(w.text for w in row)


def _gutter(lines: list[Box]) -> float | None:
    """x position of the gap between two text columns, or None for single-column text.

    A label/value table also has an empty vertical strip between its columns,
    but splitting it would separate each label from its value. Real text
    columns are told apart by their shape: lines of the left column run up to
    the gutter, and lines of the right column start at the same x.
    """
    if len(lines) < 8:
        return None
    x0, x1 = min(b.x0 for b in lines), max(b.x1 for b in lines)
    width = x1 - x0
    best, best_crossing = None, None
    for step in range(30, 71):  # candidate gutters across the middle of the page
        gx = x0 + width * step / 100
        crossing = sum(1 for b in lines if b.x0 < gx < b.x1)
        if best_crossing is None or crossing < best_crossing:
            best, best_crossing = gx, crossing
    if best is None or best_crossing > max(2, 0.1 * len(lines)):
        return None
    left = [b for b in lines if b.x1 <= best]
    right = [b for b in lines if b.x0 >= best]
    if len(left) < 4 or len(right) < 4:
        return None
    right_start = min(b.x0 for b in right)
    reaches_gutter = sum(1 for b in left if best - b.x1 < 0.12 * width) / len(left)
    starts_aligned = sum(1 for b in right if b.x0 - right_start < 0.04 * width) / len(right)
    return best if reaches_gutter >= 0.4 and starts_aligned >= 0.4 else None

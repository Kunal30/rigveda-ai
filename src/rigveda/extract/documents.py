"""PDF, Word, PowerPoint and Excel files."""

from __future__ import annotations

import sys
from pathlib import Path

from . import ExtractionError, Segment
from .pdf_layout import page_text
from .. import llm

MAX_SHEET_ROWS = 5000
VERSIONS = {"pdf": "2"}  # 2: text rebuilt in visual reading order (filled-in forms)
SCANNED_PAGE_CHARS = 25  # pages with less text than this are treated as scans


def _require(module: str, package: str):
    try:
        return __import__(module)
    except ImportError as error:
        raise ExtractionError(f"install {package} to read these files (pip install -e '.[docs]')") from error


def read_pdf(path: Path, config) -> list[Segment]:
    fitz = _require("pymupdf", "pymupdf")
    segments, ocr_failed = [], False
    with fitz.open(path) as document:
        for number, page in enumerate(document, start=1):
            text = page_text(page)
            if len(text.strip()) < SCANNED_PAGE_CHARS and page.get_images() and not ocr_failed:
                try:
                    image = page.get_pixmap(dpi=150).tobytes("png")
                    text = llm.image_text(config.ollama, config.vision_model, image)
                except llm.OllamaError as error:
                    # Keep the text layer we have; one warning per file is enough.
                    print(f"OCR unavailable for scanned pages in {path}: {error}", file=sys.stderr)
                    ocr_failed = True
            segments.append(Segment(text, f"page {number}"))
    return segments


def read_docx(path: Path, config) -> list[Segment]:
    docx = _require("docx", "python-docx")
    document = docx.Document(path)
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return [Segment("\n".join(parts))]


def read_pptx(path: Path, config) -> list[Segment]:
    pptx = _require("pptx", "python-pptx")
    segments = []
    for number, slide in enumerate(pptx.Presentation(path).slides, start=1):
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append("Speaker notes: " + notes)
        segments.append(Segment("\n".join(parts), f"slide {number}"))
    return segments


def read_xlsx(path: Path, config) -> list[Segment]:
    openpyxl = _require("openpyxl", "openpyxl")
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    segments = []
    try:
        for sheet in workbook.worksheets:
            rows = []
            for values in sheet.iter_rows(values_only=True):
                cells = ["" if value is None else str(value) for value in values]
                if any(cells):
                    rows.append(" | ".join(cells).rstrip(" |"))
                if len(rows) >= MAX_SHEET_ROWS:
                    break
            segments.append(Segment("\n".join(rows), f"sheet {sheet.title}"))
    finally:
        workbook.close()
    return segments


EXTRACTORS = [
    (".pdf", "pdf", read_pdf),
    (".docx", "document", read_docx),
    (".pptx", "slides", read_pptx),
    (".xlsx", "spreadsheet", read_xlsx),
]

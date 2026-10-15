"""Photos, screenshots and scans, read by a local vision model."""

from __future__ import annotations

import math
from pathlib import Path

from . import ExtractionError, Segment
from .. import llm

MIN_SIDE = 64  # skip icons and thumbnails
MAX_SIDE = 2000  # larger images are scaled down before being sent to the model


def _png(path: Path) -> bytes | None:
    try:
        import pymupdf as fitz
    except ImportError as error:
        raise ExtractionError("install pymupdf to read images (pip install -e '.[docs]')") from error
    pixmap = fitz.Pixmap(str(path))
    if min(pixmap.width, pixmap.height) < MIN_SIDE:
        return None
    if pixmap.alpha:
        pixmap = fitz.Pixmap(pixmap, 0)
    if max(pixmap.width, pixmap.height) > MAX_SIDE:
        pixmap.shrink(math.ceil(math.log2(max(pixmap.width, pixmap.height) / MAX_SIDE)))  # halves n times
    return pixmap.tobytes("png")


def read_image(path: Path, config) -> list[Segment]:
    image = _png(path)
    if image is None:
        return []
    try:
        return [Segment(llm.image_text(config.ollama, config.vision_model, image), "image")]
    except llm.OllamaError as error:
        raise ExtractionError(f"vision model unavailable ({error}); run `ollama pull {config.vision_model}`") from error


EXTRACTORS = [(suffix, "image", read_image) for suffix in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")]

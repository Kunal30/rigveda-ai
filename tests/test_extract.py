from pathlib import Path

import pytest

from rigveda import extract, llm
from rigveda.extract import media
from rigveda.indexer import index_paths
from rigveda.search import search


def test_pdf_pages(tmp_path: Path, config) -> None:
    fitz = pytest.importorskip("pymupdf")
    path = tmp_path / "report.pdf"
    document = fitz.open()
    for text in ("First page about budgets.", "Second page about passports."):
        document.new_page().insert_text((72, 72), text)
    document.save(path)
    segments = extract.extract(path, config)
    assert [s.location for s in segments] == ["page 1", "page 2"]
    assert "passports" in segments[1].text


def test_scanned_pdf_page_uses_vision_model(tmp_path: Path, config, monkeypatch) -> None:
    fitz = pytest.importorskip("pymupdf")
    image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
    image.clear_with(200)
    path = tmp_path / "scan.pdf"
    document = fitz.open()
    document.new_page().insert_image(fitz.Rect(0, 0, 100, 100), pixmap=image)
    document.save(path)
    monkeypatch.setattr(llm, "image_text", lambda host, model, data: "Passport number X1234567")
    assert extract.extract(path, config)[0].text == "Passport number X1234567"


def test_docx_paragraphs_and_tables(tmp_path: Path, config) -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Employment letter")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Employer", "Acme Corp"
    document.save(tmp_path / "letter.docx")
    text = extract.extract(tmp_path / "letter.docx", config)[0].text
    assert "Employment letter" in text and "Employer | Acme Corp" in text


def test_pptx_slides_and_notes(tmp_path: Path, config) -> None:
    pptx = pytest.importorskip("pptx")
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "Quarterly results"
    slide.notes_slide.notes_text_frame.text = "Mention the new office in Pune."
    deck.save(tmp_path / "deck.pptx")
    [segment] = extract.extract(tmp_path / "deck.pptx", config)
    assert segment.location == "slide 1"
    assert "Quarterly results" in segment.text and "Speaker notes: Mention the new office in Pune." in segment.text


def test_xlsx_sheets(tmp_path: Path, config) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    workbook.active.title = "Budget"
    workbook.active.append(["Item", "Cost"])
    workbook.active.append(["Laptop", 1200])
    workbook.save(tmp_path / "money.xlsx")
    [segment] = extract.extract(tmp_path / "money.xlsx", config)
    assert segment.location == "sheet Budget" and "Laptop | 1200" in segment.text


def test_image_uses_vision_model(tmp_path: Path, config, monkeypatch) -> None:
    fitz = pytest.importorskip("pymupdf")
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 120), False)
    pixmap.clear_with(255)
    pixmap.save(tmp_path / "receipt.png")
    monkeypatch.setattr(llm, "image_text", lambda host, model, data: "TOTAL 42.00\nDescription: a shop receipt")
    [segment] = extract.extract(tmp_path / "receipt.png", config)
    assert segment.location == "image" and "TOTAL 42.00" in segment.text


def test_image_is_retried_when_ollama_is_down(tmp_path: Path, config, monkeypatch) -> None:
    fitz = pytest.importorskip("pymupdf")
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 120), False)
    pixmap.clear_with(255)
    pixmap.save(tmp_path / "photo.png")

    def down(*args):
        raise llm.OllamaError("connection refused")

    monkeypatch.setattr(llm, "image_text", down)
    stats = index_paths(config.database_path, [str(tmp_path)], config, log=lambda message: None)
    assert (stats.updated, stats.skipped) == (0, 1)  # not recorded, so the next index tries again


def test_transcript_grouped_by_minute() -> None:
    pieces = [(0.0, " Hello"), (30.0, " world"), (61.0, " next minute"), (3725.0, " much later")]
    segments = media.group(pieces)
    assert [(s.location, s.text) for s in segments] == [("0:00", "Hello world"), ("1:01", "next minute"), ("1:02:05", "much later")]


def test_video_indexed_with_timestamps(tmp_path: Path, config, monkeypatch) -> None:
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    video = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2", "-f", "lavfi", "-i",
                    "color=size=64x64:duration=2", "-shortest", str(video)], check=True)
    monkeypatch.setattr(media, "transcribe", lambda samples, model: [(0.0, "Welcome to the Rigveda demo")])
    index_paths(config.database_path, [str(tmp_path)], config, log=lambda message: None)
    [row] = search(config.database_path, "demo", 3)
    assert row["location"] == "0:00" and row["path"].endswith("talk.mp4")

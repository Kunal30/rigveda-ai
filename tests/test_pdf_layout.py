from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from rigveda.extract.pdf_layout import page_text  # noqa: E402


def _filled_form(path: Path) -> None:
    """A two-column form whose typed-in values are written after the template, like e-signed leases."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    left = ["Moving in. This lease is between you and the owner of the apartment"] * 3 + ["Apartment No.            , at the address"]
    right = ["6. RENT AND CHARGES. Unless modified by addenda, you will pay"] + ["$                per month for rent, payable in advance"] + ["and without demand at the on-site manager's office here"] * 3
    for i, text in enumerate(left):
        page.insert_text((36, 100 + 14 * i), text, fontsize=8)
    for i, text in enumerate(right):
        page.insert_text((320, 103 + 14 * i), text, fontsize=8)  # baselines offset from the left column
    for i in range(4):
        page.insert_text((36, 300 + 14 * i), "More left column text that runs all the way to the gutter.", fontsize=8)
        page.insert_text((320, 303 + 14 * i), "More right column text that also runs to the right margin.", fontsize=8)
    # Values typed in last, so they are stored at the end of the page.
    page.insert_text((100, 142), "B34", fontname="cour", fontsize=9)
    page.insert_text((330, 117), "2605.00", fontname="cour", fontsize=9)
    document.save(path)


def test_filled_values_rejoin_their_labels(tmp_path: Path) -> None:
    _filled_form(tmp_path / "lease.pdf")
    lines = page_text(pymupdf.open(tmp_path / "lease.pdf")[0]).split("\n")
    assert "$ 2605.00 per month for rent, payable in advance" in lines
    assert "Apartment No. B34 , at the address" in lines


def test_columns_are_read_one_after_the_other(tmp_path: Path) -> None:
    _filled_form(tmp_path / "lease.pdf")
    text = page_text(pymupdf.open(tmp_path / "lease.pdf")[0])
    assert text.index("Apartment No.") < text.index("RENT AND CHARGES")  # whole left column first
    assert "owner of the apartment 6. RENT" not in text  # columns never merged into one line


def test_label_value_table_keeps_pairs_together(tmp_path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page()
    for i, (label, value) in enumerate([("Name", "Kunal Suthar"), ("Date of birth", "1994-01-01"), ("Passport", "X1234567"),
                                        ("City", "Sammamish"), ("State", "Washington"), ("Zip", "98075"), ("Phone", "555-0100"),
                                        ("Employer", "Acme Corp"), ("Start date", "2026-08-26")]):
        page.insert_text((50, 80 + 16 * i), label, fontsize=10)
        page.insert_text((350, 80 + 16 * i), value, fontsize=10)
    lines = page_text(page).split("\n")
    assert "Passport X1234567" in lines and "Zip 98075" in lines


def test_words_split_into_fragments_are_rejoined(tmp_path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page()
    writer = pymupdf.TextWriter(page.rect)
    font = pymupdf.Font("helv")
    x = 72
    for fragment in ("R", "eturn ", "to", " sender"):  # separate spans, as kerned PDFs store text
        writer.append((x, 100), fragment, font=font, fontsize=11)
        x += font.text_length(fragment, fontsize=11)
    writer.write_text(page)
    assert page_text(page) == "Return to sender"

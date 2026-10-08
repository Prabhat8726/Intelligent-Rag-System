"""Table detection: hand-built geometry for each rule, generated documents against ground truth."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from docintel.processing.content import PageContent, Word
from docintel.processing.inspection import PageMethod
from docintel.processing.layout import analyze_layout
from docintel.processing.native import extract_native_words
from docintel.processing.pdf import open_pdf
from docintel.processing.tables import stitch_tables
from docintel.synthetic.catalog import LOCALES, VENDORS
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.render import format_amount, format_quantity
from docintel.synthetic.scenarios import Scenario
from tests.unit.test_layout import word, words_on_line

# Column x positions of a typical line-item table: headers left-aligned at these positions.
COLUMNS = {"#": 40, "Item": 60, "Description": 130, "Qty": 330, "Unit": 380, "Price": 430}


def right_aligned(text: str, column_end: float, y: float) -> Word:
    width = len(text) * 5.0
    return word(text, column_end - width, y)


def table_words(rows: list[tuple[str, str, str, str, str, str]], top: float = 200) -> list[Word]:
    words: list[Word] = []
    for name, x in COLUMNS.items():
        words.append(word(name, x, top))
    for index, (number, sku, description, qty, unit, price) in enumerate(rows):
        y = top + (index + 1) * 17
        words.append(word(number, COLUMNS["#"], y))
        words.append(word(sku, COLUMNS["Item"], y))
        words += words_on_line(description, COLUMNS["Description"], y)
        words.append(right_aligned(qty, COLUMNS["Unit"] - 4, y))  # numbers hug the column end
        words.append(word(unit, COLUMNS["Unit"], y))
        words.append(right_aligned(price, 520, y))
    return words


def page_of(words: list[Word], number: int = 1) -> PageContent:
    return analyze_layout(PageContent(number, 600, 800, "pt", PageMethod.NATIVE, words))


ROWS = [
    ("1", "BRG-6204", "Deep groove ball bearing", "150", "pcs", "4.85"),
    ("2", "VLV-BL050", "Brass ball valve DN50", "5", "pcs", "38.40"),
]


def test_left_aligned_headers_with_right_aligned_numbers() -> None:
    page = page_of(table_words(ROWS))
    assert len(page.tables) == 1
    table = page.tables[0]
    assert table.header == list(COLUMNS)
    assert [row.cells for row in table.rows] == [list(row) for row in ROWS]
    assert table.confidence == 1.0


def test_wrapped_cell_continues_the_row() -> None:
    words = table_words(ROWS)
    # A second description line directly below row 1 (normal line spacing, before row 2 moves).
    words = [w for w in words if w.bbox.y0 < 230]  # header + row 1
    words += words_on_line("sealed both sides", COLUMNS["Description"], 228)
    words += table_words([ROWS[1]], top=228)[len(COLUMNS) :]  # row 2 below the wrapped line
    page = page_of(words)
    cells = [row.cells for row in page.tables[0].rows]
    assert cells[0][2] == "Deep groove ball bearing sealed both sides"
    assert cells[1][0] == "2"


def test_table_stops_before_totals_and_footer() -> None:
    words = table_words(ROWS)
    words += words_on_line("Subtotal", 380, 280)  # well below the row rhythm
    words.append(right_aligned("1,019.00", 520, 280))
    words += words_on_line("Thank you for your business", 40, 330)
    page = page_of(words)
    assert len(page.tables[0].rows) == 2
    assert "Subtotal  1,019.00" in page.text
    assert page.text.endswith("Thank you for your business")


def test_address_lines_are_not_a_table() -> None:
    words: list[Word] = []
    for row, cells in enumerate(
        [("1840 Foundry Road", "Invoice No.", "INV-1"), ("Dayton OH 45402", "Date", "2026-01-02")]
    ):
        for text, x in zip(cells, (40, 300, 420), strict=True):
            words += words_on_line(text, x, 100 + row * 15)
    assert page_of(words).tables == []


def test_tables_continued_under_the_same_header_are_stitched() -> None:
    first = page_of(table_words(ROWS), number=1)
    second = page_of(table_words([("3", "GSK-150A", "Graphite gasket", "4", "pcs", "6.20")]), 2)
    other = page_of(
        [word(h, x, 200) for h, x in (("Date", 40), ("Reference", 120), ("Amount", 300))]
        + [word("01.02.", 40, 217), word("REF-9", 120, 217), word("10.00", 300, 217)],
        number=3,
    )
    tables = stitch_tables([first, second, other])
    assert [(t.page_start, t.page_end, len(t.rows)) for t in tables] == [(1, 2, 3), (3, 3, 1)]
    assert [row.page_number for row in tables[0].rows] == [1, 1, 2]
    assert tables[0].method == "NATIVE"


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("tables-dataset")
    generate_dataset(
        output,
        seed=11,
        scenarios=[Scenario.CLEAN_MATCH, Scenario.VENDOR_NAME_VARIANT, Scenario.LONG_MULTIPAGE],
    )
    return output


def test_native_line_item_tables_match_ground_truth(generated: Path) -> None:
    manifest = json.loads((generated / "manifest.json").read_text())
    checked = 0
    for entry in manifest["documents"]:
        if entry["variant"] != "native":
            continue
        truth = json.loads((generated / entry["ground_truth"]).read_text())
        pages = []
        with open_pdf(generated / entry["file"]) as document:
            for index in range(len(document)):
                words, width, height = extract_native_words(document[index])
                pages.append(
                    analyze_layout(
                        PageContent(index + 1, width, height, "pt", PageMethod.NATIVE, words)
                    )
                )
        tables = stitch_tables(pages)
        assert len(tables) == 1, entry["file"]
        locale = LOCALES[
            next(v for v in VENDORS if v.code == truth["fields"]["vendor_code"]).country
        ]
        expected = []
        for item in truth["line_items"]:
            row = [
                str(item["line_number"]),
                item["sku"],
                item["description"],
                format_quantity(Decimal(item["quantity"])),
                item["unit"],
            ]
            if "unit_price" in item:
                row += [
                    format_amount(Decimal(item["unit_price"]), locale),
                    format_amount(Decimal(item["line_total"]), locale),
                ]
            expected.append(row)
        assert [row.cells for row in tables[0].rows] == expected, entry["file"]
        checked += 1
    assert checked >= 9

"""Evidence location and the deterministic layout extractor."""

from __future__ import annotations

from docintel.db.models import DocumentType
from docintel.fields.evidence import EvidenceLocator, EvidenceStatus
from docintel.fields.local import LayoutExtractor, map_columns, numeric_cell
from docintel.fields.schemas import SCHEMA_INFO
from docintel.processing.content import BBox, DocumentTable, PageContent, TableRow, Word
from docintel.processing.inspection import PageMethod
from docintel.processing.layout import analyze_layout
from docintel.processing.tables import stitch_tables
from tests.factories.pages import invoice_page, make_page

INVOICE = SCHEMA_INFO[DocumentType.INVOICE]
PURCHASE_ORDER = SCHEMA_INFO[DocumentType.PURCHASE_ORDER]


# ------------------------------------------------------------------------------ evidence
def test_quote_is_found_regardless_of_case_and_spacing() -> None:
    page = invoice_page()
    locator = EvidenceLocator([page])
    evidence = locator.locate("total due  $135.64", cited_page=1)
    assert evidence.status == EvidenceStatus.VERIFIED
    assert evidence.page == 1
    assert evidence.matched_text == "Total Due $ 135.64"
    assert evidence.bbox is not None
    assert evidence.bbox.x0 >= 320
    assert evidence.ocr_confidence is None  # native text


def test_quotes_align_to_word_boundaries() -> None:
    page = make_page([[(50, "Year 2010 report")], [(50, "Qty 10")]])
    evidence = EvidenceLocator([page]).locate("10")
    assert evidence.status == EvidenceStatus.VERIFIED
    assert evidence.matched_text == "10"
    assert evidence.bbox is not None
    assert evidence.bbox.y0 > page.words[0].bbox.y0  # line 2


def test_near_picks_the_occurrence_inside_the_row() -> None:
    page = invoice_page()
    row_two = BBox(40, 0, 560, 10_000)
    rows = [w for w in page.words if w.text == "2" and w.bbox.x0 > 300]  # quantity of row 2
    assert rows
    evidence = EvidenceLocator([page]).locate("2", near=rows[0].bbox)
    assert evidence.bbox == rows[0].bbox
    assert EvidenceLocator([page]).locate("2", near=row_two).status == EvidenceStatus.VERIFIED


def test_ocr_noise_is_fuzzy_and_absent_quotes_are_not_found() -> None:
    page = make_page([[(50, "Invoice No. lNV-2O26-0042")]], ocr_confidence=71.0)
    locator = EvidenceLocator([page])
    fuzzy = locator.locate("Invoice No. INV-2026-0042")
    assert fuzzy.status == EvidenceStatus.FUZZY
    assert 85 <= fuzzy.score < 100
    assert fuzzy.ocr_confidence == 71.0
    assert locator.locate("Total Due $ 99.00").status == EvidenceStatus.NOT_FOUND
    assert locator.locate("lNV").status == EvidenceStatus.VERIFIED
    assert locator.locate("xyz").status == EvidenceStatus.NOT_FOUND  # short: exact only


def test_wrong_page_citation_is_recorded() -> None:
    first = make_page([[(50, "Terms and conditions")]], page_number=1)
    second = make_page([[(50, "Total Due $ 135.64")]], page_number=2)
    evidence = EvidenceLocator([first, second]).locate("Total Due $ 135.64", cited_page=1)
    assert evidence.status == EvidenceStatus.VERIFIED
    assert evidence.page == 2
    assert evidence.page_matches_citation is False


# ------------------------------------------------------------------------------ layout extractor
def test_invoice_header_fields_line_items_and_derived_currency() -> None:
    page = invoice_page()
    output = LayoutExtractor(INVOICE).extract([page], stitch_tables([page]))
    values = {name: candidate.raw_value for name, candidate in output.scalars.items()}
    assert values == {
        "vendor_name": "Kestrel Industrial Supply Inc.",
        "vendor_tax_id": "US-47-2917735",
        "invoice_number": "INV-2026-0042",
        "invoice_date": "03/14/2026",
        "due_date": "04/13/2026",
        "purchase_order_number": "PO-2026-10001",
        "buyer_name": "Meridian Manufacturing Co.",
        "payment_terms_days": "Net 30 days",
        "subtotal": "$ 125.30",
        "tax_amount": "$ 10.34",
        "tax_rate": "8.25%",
        "total": "$ 135.64",
        "currency": "$",
    }
    assert output.scalars["invoice_number"].anchor == 1.0
    assert output.scalars["buyer_name"].anchor == 0.95  # value below its label
    assert output.scalars["vendor_name"].anchor == 0.9  # letterhead
    assert output.scalars["currency"].anchor == 0.7  # assumed from "$"
    assert output.scalars["total"].source_text == "Total Due $ 135.64"
    assert [row.cells["sku"].raw_value for row in output.rows] == ["BRG-6204", "VLV-BL050"]
    assert output.rows[1].cells["amount"].raw_value == "76.80"
    assert output.rows[1].cells["amount"].bbox is not None


def test_po_letterhead_is_a_weak_vendor_hint() -> None:
    page = invoice_page()
    output = LayoutExtractor(PURCHASE_ORDER).extract([page], [])
    assert output.scalars["vendor_name"].anchor == 0.75


def test_letterhead_falls_back_to_the_first_line_when_nothing_stands_out() -> None:
    page = make_page(
        [
            [(50, "INVOICE")],
            [(50, "Acme Widgets Ltd")],
            [(50, "Invoice No. A-1")],
            [(52, "Qty"), (140, "Description"), (330, "Amount")],
            [(52, "1"), (140, "Widget"), (330, "5.00")],
        ]
    )
    candidate = (
        LayoutExtractor(INVOICE).extract([page], stitch_tables([page])).scalars["vendor_name"]
    )
    assert candidate.raw_value == "Acme Widgets Ltd"  # not the title, not a table header
    assert candidate.anchor == 0.72


def test_one_word_label_needs_a_colon_or_a_value() -> None:
    page = make_page([[(50, "Tax ID: GB 284 7712 09")], [(50, "Total Due"), (300, "£ 10.00")]])
    output = LayoutExtractor(INVOICE).extract([page], [])
    assert "tax_amount" not in output.scalars  # "Tax ID" is not the label "tax"
    assert output.scalars["vendor_tax_id"].raw_value == "GB 284 7712 09"
    assert output.scalars["total"].raw_value == "£ 10.00"


def test_label_and_value_split_across_lines_by_skew() -> None:
    # The value sits 6pt lower than its label (a rotated scan): too little overlap to share a
    # line, but level enough to be read as the label's value.
    words = [
        Word("Bristol", BBox(50, 60, 80, 69), 95.0, 9.0),
        Word("Invoice", BBox(320, 60, 352, 69), 95.0, 9.0),
        Word("No.", BBox(355, 60, 368, 69), 95.0, 9.0),
        Word("INV-77", BBox(420, 66, 450, 75), 95.0, 9.0),
    ]
    page = analyze_layout(
        PageContent(1, 595, 842, "pt", PageMethod.OCR, words, ocr_confidence=95.0)
    )
    assert len(page.lines) == 2
    candidate = LayoutExtractor(INVOICE).extract([page], []).scalars["invoice_number"]
    assert candidate.raw_value == "INV-77"
    assert candidate.method == "right of (adjacent line) label 'Invoice No.'"


def test_repeated_labels_take_the_last_total_and_count_conflicts() -> None:
    page = make_page(
        [
            [(50, "Total"), (300, "$ 10.00")],
            [(50, "Carried forward")],
            [(50, "Total"), (300, "$ 25.00")],
        ]
    )
    candidate = LayoutExtractor(INVOICE).extract([page], []).scalars["total"]
    assert candidate.raw_value == "$ 25.00"
    assert candidate.conflicts == 1


def _table(header: list[str], rows: list[list[str]]) -> DocumentTable:
    box = BBox(0, 0, 500, 100)
    return DocumentTable(
        1, 1, box, header, [TableRow(cells, box, 1) for cells in rows], "NATIVE", 0.9
    )


def test_column_mapping_by_header_and_content() -> None:
    columns = INVOICE.table.columns if INVOICE.table else ()
    mapped = map_columns(
        _table(
            ["", "Item", "Qty", "Price", "Amount"],
            [
                ["1", "Steel bolts box", "4", "2.50", "10.00"],
                ["2", "Washers pack", "1", "3.00", "3.00"],
            ],
        ),
        columns,
    )
    assert mapped is not None
    names = {index: name for index, (name, _) in mapped[0].items()}
    # Unlabelled 1, 2, ... is the line number; an "Item" column of prose is the description.
    assert names == {
        0: "line_number",
        1: "description",
        2: "quantity",
        3: "unit_price",
        4: "amount",
    }
    assert map_columns(_table(["Date", "Event", "Owner"], [["x", "y", "z"]]), columns) is None


def test_junk_rows_are_dropped_and_merged_quantity_units_split() -> None:
    page = make_page([[(50, "x")]])
    table = _table(
        ["Description", "Qty", "Unit Price", "Amount"],
        [
            ["Hex bolt", "10 pcs", "1.20", "12.00"],
            ["Please remit", "payment within", "30 days", "net"],
            ["Total Due", "12.00", "USD", ""],
        ],
    )
    output = LayoutExtractor(INVOICE).extract([page], [table])
    assert len(output.rows) == 1
    cells = {name: c.raw_value for name, c in output.rows[0].cells.items()}
    assert cells == {
        "description": "Hex bolt",
        "quantity": "10",
        "unit": "pcs",
        "unit_price": "1.20",
        "amount": "12.00",
    }


def test_numeric_cells_hold_exactly_one_number() -> None:
    assert numeric_cell("$ 1,056.50")
    assert numeric_cell("1.234,56 EUR")
    assert not numeric_cell("zinc 5")
    assert not numeric_cell("14.31 71.55")
    assert not numeric_cell("pcs")


def test_resume_skills_listed_under_a_heading() -> None:
    page = make_page(
        [
            [(50, "Jordan Example")],
            [(50, "Email: jordan@example.test")],
            [],
            [(50, "Skills")],
            [(50, "Python, SQL, Docker")],
            [(50, "Kubernetes; Terraform")],
        ],
        sizes={"Jordan Example": 16.0},
    )
    output = LayoutExtractor(SCHEMA_INFO[DocumentType.RESUME]).extract([page], [])
    assert output.scalars["candidate_name"].raw_value == "Jordan Example"
    assert output.scalars["email"].raw_value == "jordan@example.test"
    assert [item.raw_value for item in output.lists["skills"]] == [
        "Python",
        "SQL",
        "Docker",
        "Kubernetes",
        "Terraform",
    ]


def test_statement_period_range_feeds_both_dates() -> None:
    page = make_page(
        [
            [(50, "Example Bank")],
            [(50, "Statement period"), (200, "01/03/2026 - 31/03/2026")],
            [(50, "Opening balance"), (200, "1,000.00")],
        ],
        sizes={"Example Bank": 15.0},
    )
    output = LayoutExtractor(SCHEMA_INFO[DocumentType.BANK_STATEMENT]).extract([page], [])
    assert output.scalars["statement_period_start"].raw_value == "01/03/2026"
    assert output.scalars["statement_period_end"].raw_value == "31/03/2026"
    assert output.scalars["bank_name"].raw_value == "Example Bank"

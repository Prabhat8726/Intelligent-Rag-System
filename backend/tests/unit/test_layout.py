"""Layout analysis on hand-built word geometry (independent of PDF or OCR engines)."""

from __future__ import annotations

from docintel.processing.content import BBox, BlockKind, PageContent, Word
from docintel.processing.inspection import PageMethod
from docintel.processing.layout import analyze_layout, build_lines

SIZE = 10.0


def word(text: str, x: float, y: float, *, size: float = SIZE, char_width: float = 5.0) -> Word:
    """A word whose box starts at (x, y) - top-left origin - sized from its text."""
    return Word(text, BBox(x, y, x + len(text) * char_width, y + size), None, size)


def words_on_line(text: str, x: float, y: float, *, size: float = SIZE) -> list[Word]:
    """Words separated by single spaces (gap 0.3 x size: inside one segment)."""
    result: list[Word] = []
    for token in text.split():
        result.append(word(token, x, y, size=size))
        x = result[-1].bbox.x1 + 0.3 * size
    return result


def page_of(words: list[Word]) -> PageContent:
    return analyze_layout(PageContent(1, 600, 800, "pt", PageMethod.NATIVE, words))


def line_texts(page: PageContent) -> list[list[str]]:
    return [
        [" ".join(page.words[i].text for i in segment) for segment in line.segments]
        for line in page.lines
    ]


def test_words_are_grouped_into_lines_and_column_segments() -> None:
    words = [
        *words_on_line("Invoice No.", 50, 100),
        *words_on_line("INV-1001", 200, 101),  # column-sized gap, baseline jitter
        *words_on_line("Due Date", 50, 120),
    ]
    page = page_of(words)
    assert line_texts(page) == [["Invoice No.", "INV-1001"], ["Due Date"]]


def test_skewed_lines_are_kept_whole_when_skew_is_known() -> None:
    slope = 0.03  # ~1.7 degrees, typical of a scan
    words = [word(f"w{i}", 50 + i * 60, 100 + i * 60 * slope) for i in range(8)]
    words += [word(f"v{i}", 50 + i * 60, 118 + i * 60 * slope) for i in range(8)]
    assert len(build_lines(words, skew=slope)) == 2
    assert len(build_lines(words)) > 2  # without de-skewing the slope breaks the lines


def test_label_value_grid_reads_row_by_row() -> None:
    labels = ["Invoice No.", "Invoice Date", "Currency"]
    values = ["INV-1001", "2026-03-04", "USD"]
    words: list[Word] = []
    for row, (label, value) in enumerate(zip(labels, values, strict=True)):
        words += words_on_line(label, 300, 100 + row * 17)
        words += words_on_line(value, 400, 100 + row * 17)
    text = page_of(words).text
    assert "Invoice No.  INV-1001\nInvoice Date  2026-03-04\nCurrency  USD" in text


def test_independent_columns_stay_separate_blocks() -> None:
    words: list[Word] = []
    for row, (left, right) in enumerate(
        [
            ("1840 Foundry Road", "Payment Terms"),
            ("Dayton OH", "Currency"),
            ("United States", "Bank"),
        ]
    ):
        words += words_on_line(left, 50, 100 + row * 12)
        words += words_on_line(right, 420, 100 + row * 12)
    page = page_of(words)
    assert page.text.split("\n\n") == [
        "1840 Foundry Road\nDayton OH\nUnited States",
        "Payment Terms\nCurrency\nBank",
    ]


def test_a_wide_space_inside_a_sentence_does_not_move_the_rest_of_the_line() -> None:
    # OCR word boxes are tight: a space can measure wider than the column threshold. The tail
    # must stay on its line, not be read after the whole paragraph.
    first = words_on_line("The Supplier shall maintain insurance of at least", 50, 100)
    tail = words_on_line("1,000,000 USD per", first[-1].bbox.x1 + 0.9 * SIZE, 100)
    words = [
        *first,
        *tail,
        *words_on_line("claim.", 50, 112),
        *words_on_line("5. Non-Solicitation", 50, 124),
    ]
    page = page_of(words)
    assert line_texts(page)[0] == [
        "The Supplier shall maintain insurance of at least",
        "1,000,000 USD per",
    ]
    assert page.text.splitlines()[:2] == [
        "The Supplier shall maintain insurance of at least  1,000,000 USD per",
        "claim.",
    ]


def test_far_numeric_column_still_pairs_with_its_labels() -> None:
    words: list[Word] = []
    for row, (label, amount) in enumerate([("Subtotal", "$ 962.55"), ("Total Due", "$ 1,041.96")]):
        words += words_on_line(label, 300, 300 + row * 15)
        words += words_on_line(amount, 520, 300 + row * 15)
    assert "Subtotal  $ 962.55\nTotal Due  $ 1,041.96" in page_of(words).text


def test_heading_and_paragraph_breaks() -> None:
    words = [
        *words_on_line("INVOICE", 50, 50, size=20),
        *words_on_line("first paragraph line", 50, 100),
        *words_on_line("second paragraph line", 50, 112),
        *words_on_line("after a break", 50, 160),
    ]
    page = page_of(words)
    kinds = [block.kind for block in page.blocks]
    assert kinds == [BlockKind.HEADING, BlockKind.TEXT, BlockKind.TEXT]
    assert page.text == "INVOICE\n\nfirst paragraph line\nsecond paragraph line\n\nafter a break"


def test_empty_page() -> None:
    page = page_of([])
    assert page.lines == []
    assert page.blocks == []
    assert page.text == ""

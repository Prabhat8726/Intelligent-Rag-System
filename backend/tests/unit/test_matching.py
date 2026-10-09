"""Comparison engine and near-duplicate detection (Modules 9, 29) on hand-built facts."""

from __future__ import annotations

from decimal import Decimal

from docintel.db.models import ComparisonType, DocumentType
from docintel.matching import compare as checks
from docintel.matching.compare import (
    ItemStatus,
    Tolerances,
    compare_delivery,
    compare_invoice,
)
from docintel.matching.duplicates import DuplicateKind, find_duplicates
from docintel.matching.facts import reference_key
from tests.factories.facts import delivery, document, invoice, line, purchase_order

TOL = Tolerances()
ORDER = [line(0, "BRG-6204", 10, "4.85"), line(1, "VLV-BL050", 2, "38.40")]


def item(outcome: checks.ComparisonOutcome, key: str) -> checks.ComparisonItem:
    return next(entry for entry in outcome.items if entry.key == key)


def test_clean_three_way_match_has_only_matches() -> None:
    outcome = compare_invoice(
        invoice([line(0, "BRG-6204", 10, "4.85"), line(1, "VLV-BL050", 2, "38.40")]),
        purchase_order(ORDER),
        [delivery([line(0, "BRG-6204", 10), line(1, "VLV-BL050", 2)])],
        TOL,
    )
    assert outcome.comparison_type == ComparisonType.INVOICE_PO_DELIVERY
    assert {entry.status for entry in outcome.items} == {ItemStatus.MATCH}
    assert outcome.summary["MATCH"] == len(outcome.items)
    checks_run = {entry.check for entry in outcome.items}
    assert {
        checks.VENDOR,
        checks.CURRENCY,
        checks.PO_REFERENCE,
        checks.TAX_RATE,
        checks.PAYMENT_TERMS,
        checks.UNIT_PRICE,
        checks.QUANTITY_ORDERED,
        checks.QUANTITY_DELIVERED,
    } <= checks_run


def test_price_mismatch_carries_evidence_from_both_documents() -> None:
    bill = invoice([line(0, "BRG-6204", 10, "4.85"), line(1, "VLV-BL050", 2, "41.47")])
    order = purchase_order(ORDER)
    outcome = compare_invoice(bill, order, [], TOL)
    assert outcome.comparison_type == ComparisonType.INVOICE_PO
    price = item(outcome, "line:VLV-BL050:unit_price")
    assert price.status == ItemStatus.MISMATCH
    assert (price.left_value, price.right_value) == ("41.47", "38.4")
    assert price.difference == {"absolute": "+3.07", "relative": "+0.0799"}
    assert price.explanation == (
        "Unit price of VLV-BL050: invoice 41.47 USD, purchase order 38.40 USD "
        "(difference +3.07, +8.0%)."
    )
    (left,), (right,) = price.left, price.right
    assert left.document_id == str(bill.document_id)
    assert right.document_id == str(order.document_id)
    assert left.field_id == bill.lines[1].cells["unit_price"].field_id
    assert (left.page, right.page) == (1, 1)
    assert left.source_text is not None
    assert item(outcome, "line:BRG-6204:unit_price").status == ItemStatus.MATCH


def test_tolerances_decide_what_is_equal() -> None:
    bill = invoice([line(0, "BRG-6204", 10, "4.90")])
    order = purchase_order([line(0, "BRG-6204", 10, "4.85")])
    strict = compare_invoice(bill, order, [], TOL)
    assert item(strict, "line:BRG-6204:unit_price").status == ItemStatus.MISMATCH
    lenient = compare_invoice(bill, order, [], Tolerances(price_pct=Decimal("0.02")))
    price = item(lenient, "line:BRG-6204:unit_price")
    assert price.status == ItemStatus.MATCH
    assert "within tolerance" in price.explanation


def test_delivered_quantities_are_summed_over_delivery_notes() -> None:
    bill = invoice([line(0, "BRG-6204", 10, "4.85")])
    notes = [
        delivery([line(0, "BRG-6204", 4)], number="DN-1"),
        delivery([line(0, "BRG-6204", 4)], number="DN-2"),
    ]
    outcome = compare_invoice(bill, purchase_order(ORDER[:1]), notes, TOL)
    delivered = item(outcome, "line:BRG-6204:quantity_vs_delivered")
    assert delivered.status == ItemStatus.MISMATCH
    assert (delivered.left_value, delivered.right_value) == ("10", "8")
    assert len(delivered.right) == 2
    assert delivered.difference is not None
    assert delivered.difference["absolute"] == "+2"


def test_lines_missing_on_either_side() -> None:
    bill = invoice([line(0, "BRG-6204", 10, "4.85"), line(1, "GSK-150A", 1, "6.58")])
    outcome = compare_invoice(bill, purchase_order(ORDER), [], TOL)
    extra = item(outcome, "line:GSK-150A:line_on_order")
    assert extra.status == ItemStatus.MISSING
    assert extra.explanation == "GSK-150A is on the invoice but not on the purchase order."
    open_line = item(outcome, "line:VLV-BL050:line_fulfilled")
    assert open_line.status == ItemStatus.MISSING
    assert open_line.left == []
    assert open_line.right[0].value == "VLV-BL050"


def test_lines_are_uncertain_when_no_line_was_read_on_the_other_side() -> None:
    # An order or delivery note without any line is an extraction gap (e.g. a table OCR could
    # not reconstruct), not a set of discrepancies: verify by hand, do not fail.
    bill = invoice([line(0, "BRG-6204", 10, "4.85")])
    outcome = compare_invoice(bill, purchase_order([]), [delivery([])], TOL)
    on_order = item(outcome, "line:BRG-6204:line_on_order")
    assert on_order.status == ItemStatus.UNCERTAIN
    assert on_order.explanation == (
        "BRG-6204 is on the invoice but no line items could be read on the purchase order."
    )
    delivered = item(outcome, "line:BRG-6204:line_delivered")
    assert delivered.status == ItemStatus.UNCERTAIN
    open_line = item(
        compare_invoice(invoice([]), purchase_order(ORDER), [], TOL),
        "line:VLV-BL050:line_fulfilled",
    )
    assert open_line.status == ItemStatus.UNCERTAIN
    assert "no line items could be read on the invoice" in open_line.explanation


def test_lines_without_sku_pair_by_description() -> None:
    bill = invoice([line(0, None, 10, "4.85", description="Deep groove ball bearing 6204")])
    order = purchase_order(
        [line(0, "BRG-6204", 10, "4.85", description="Deep groove ball bearing")]
    )
    outcome = compare_invoice(bill, order, [], TOL)
    assert not outcome.by_check(checks.LINE_ON_ORDER)
    assert outcome.by_check(checks.UNIT_PRICE)[0].status == ItemStatus.MATCH


def test_a_difference_read_with_low_confidence_is_uncertain_until_corrected() -> None:
    weak = invoice([line(0, "BRG-6204", 10, "4.65", confidence=0.7)])
    outcome = compare_invoice(weak, purchase_order(ORDER[:1]), [], TOL)
    price = item(outcome, "line:BRG-6204:unit_price")
    assert price.status == ItemStatus.UNCERTAIN
    assert "may be a misread" in price.explanation

    corrected = invoice([line(0, "BRG-6204", 10, "4.65", confidence=0.7)])
    cell = corrected.lines[0].cells["unit_price"]
    corrected.lines[0].cells["unit_price"] = type(cell)(
        **{**{slot: getattr(cell, slot) for slot in cell.__slots__}, "corrected": True}
    )
    outcome = compare_invoice(corrected, purchase_order(ORDER[:1]), [], TOL)
    assert item(outcome, "line:BRG-6204:unit_price").status == ItemStatus.MISMATCH


def test_different_currencies_are_not_converted() -> None:
    outcome = compare_invoice(
        invoice([line(0, "BRG-6204", 10, "4.85")], currency="EUR"),
        purchase_order(ORDER[:1]),
        [],
        TOL,
    )
    assert item(outcome, "currency").status == ItemStatus.MISMATCH
    price = item(outcome, "line:BRG-6204:unit_price")
    assert price.status == ItemStatus.UNCERTAIN
    assert "currencies differ" in price.explanation


def test_vendor_identity_by_master_id_else_by_name() -> None:
    other = compare_invoice(
        invoice(ORDER, vendor="Bluepeak Office Solutions LLC", vendor_id="other"),
        purchase_order(ORDER),
        [],
        TOL,
    )
    assert item(other, "vendor").status == ItemStatus.MISMATCH
    unresolved = compare_invoice(
        invoice(ORDER, vendor="KESTREL INDUSTRIAL SUPPLY, INC", vendor_id=None),
        purchase_order(ORDER, vendor="Kestrel Industrial Supply Inc.", vendor_id=None),
        [],
        TOL,
    )
    assert item(unresolved, "vendor").status == ItemStatus.MATCH


def test_delivery_against_order() -> None:
    outcome = compare_delivery(
        delivery([line(0, "BRG-6204", 6), line(1, "XYZ-1", 1)]), purchase_order(ORDER), TOL
    )
    assert outcome.comparison_type == ComparisonType.PO_DELIVERY
    short = item(outcome, "line:BRG-6204:quantity_vs_ordered")
    assert short.status == ItemStatus.MISMATCH
    assert short.difference is not None
    assert short.difference["absolute"] == "-4"
    assert item(outcome, "line:XYZ-1:line_on_order").status == ItemStatus.MISSING
    assert item(outcome, "line:VLV-BL050:line_fulfilled").status == ItemStatus.MISSING
    assert not outcome.by_check(checks.UNIT_PRICE)


def test_reference_keys_ignore_punctuation_and_case() -> None:
    assert reference_key("inv-2026/0042 ") == "INV20260042"
    assert reference_key("INV 2026 0042") == "INV20260042"
    assert reference_key("--") is None


# ------------------------------------------------------------------------------ duplicates
def test_same_vendor_and_number_is_a_strong_duplicate_of_the_older_document() -> None:
    first = invoice(ORDER, number="INV-2026-0042", total="125.30", created_minutes=0)
    again = invoice(ORDER, number="INV 2026 0042", total="125.30", created_minutes=5)
    matches = find_duplicates(again, [first])
    assert [(m.kind, m.document_id) for m in matches] == [
        (DuplicateKind.SAME_VENDOR_AND_NUMBER, str(first.document_id))
    ]
    assert matches[0].strong
    assert matches[0].evidence["other"]["number"] == "INV-2026-0042"
    assert find_duplicates(first, [again]) == []  # the original is not the duplicate


def test_same_amount_and_date_within_the_window_is_a_possible_duplicate() -> None:
    first = invoice(ORDER, number="INV-1", total="125.30", issued="2026-03-14")
    again = invoice(ORDER, number="INV-9", total="125.30", issued="2026-03-18", created_minutes=1)
    (match,) = find_duplicates(again, [first], date_window_days=7)
    assert match.kind == DuplicateKind.SAME_VENDOR_AMOUNT_AND_DATE
    assert not match.strong
    assert find_duplicates(again, [first], date_window_days=2) == []
    assert find_duplicates(again, [first], match_amount_and_date=False) == []


def test_other_vendors_and_types_are_never_duplicates() -> None:
    first = invoice(ORDER, number="INV-1", total="125.30")
    other_vendor = invoice(
        ORDER, number="INV-1", total="125.30", vendor="Bluepeak", vendor_id="b", created_minutes=1
    )
    order = document(DocumentType.PURCHASE_ORDER, number="INV-1", created_minutes=2)
    assert find_duplicates(other_vendor, [first]) == []
    assert find_duplicates(order, [first]) == []

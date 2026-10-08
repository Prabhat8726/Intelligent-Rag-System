"""Schemas, consistency checks and the confidence model."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from docintel.ai.schema import inline_refs, json_schema_for
from docintel.db.models import DocumentType
from docintel.fields.confidence import (
    ReviewLevel,
    Thresholds,
    document_confidence,
    field_confidence,
    ocr_factor,
    review_level,
)
from docintel.fields.schemas import SCHEMA_INFO, ValueType, schema_by_name, schema_for
from docintel.fields.validation import CheckStatus, consistency_by_field, run_checks


# ------------------------------------------------------------------------------ schemas
def test_every_extractable_type_has_a_described_schema() -> None:
    assert schema_for(DocumentType.OTHER) is None
    assert schema_for(None) is None
    for doc_type in DocumentType:
        if doc_type == DocumentType.OTHER:
            continue
        info = schema_for(doc_type)
        assert info is not None
        assert info.document_type == doc_type
        assert info.required_fields, doc_type
        assert schema_by_name(info.name) is info
        for scalar in info.scalars:
            assert scalar.description
            assert scalar.description != scalar.name


def test_invoice_schema_shape() -> None:
    info = SCHEMA_INFO[DocumentType.INVOICE]
    assert (info.name, info.version) == ("invoice", 1)
    assert info.required_fields == ("vendor_name", "invoice_number", "invoice_date", "total")
    assert info.table is not None
    assert info.table.name == "line_items"
    assert [c.name for c in info.table.columns] == [
        "line_number",
        "sku",
        "description",
        "quantity",
        "unit",
        "unit_price",
        "amount",
    ]
    vendor = info.scalar("vendor_name")
    assert vendor is not None
    assert vendor.meta.vendor
    assert vendor.meta.type == ValueType.ORGANIZATION


def test_model_facing_json_schema_is_self_contained() -> None:
    for info in SCHEMA_INFO.values():
        schema = json_schema_for(info.model)
        text = json.dumps(schema)
        assert "$ref" not in text
        assert "$defs" not in text
        # Extraction metadata (labels) never leaks into what the model sees.
        assert "labels" not in text
        assert "letterhead" not in text
    invoice = json_schema_for(SCHEMA_INFO[DocumentType.INVOICE].model)
    total = invoice["properties"]["total"]["anyOf"][0]
    assert set(total["required"]) == {"value", "page", "source_text"}


def test_inline_refs_rejects_runaway_nesting() -> None:
    loop = {"$defs": {"A": {"properties": {"a": {"$ref": "#/$defs/A"}}}}, "$ref": "#/$defs/A"}
    with pytest.raises(ValueError, match="too deep"):
        inline_refs(loop)


# ------------------------------------------------------------------------------ checks
ROWS = [
    {"quantity": "10", "unit_price": "4.85", "amount": "48.50"},
    {"quantity": "2", "unit_price": "38.40", "amount": "76.80"},
]
GOOD = {
    "subtotal": "125.30",
    "tax_amount": "10.34",
    "tax_rate": "0.0825",
    "total": "135.64",
    "invoice_date": "2026-03-14",
    "due_date": "2026-04-13",
    "payment_terms_days": 30,
}


def test_consistent_invoice_passes_every_check() -> None:
    checks = run_checks(GOOD, ROWS, table="line_items")
    assert {c.code for c in checks} == {
        "LINE_AMOUNT",
        "LINES_SUM",
        "TOTAL_ARITHMETIC",
        "TAX_RATE",
        "DUE_DATE_TERMS",
    }
    assert all(c.status == CheckStatus.PASS for c in checks)


def test_wrong_total_fails_only_the_total() -> None:
    checks = run_checks({**GOOD, "total": "235.64"}, ROWS, table="line_items")
    failed = [c for c in checks if c.status == CheckStatus.FAIL]
    assert [c.code for c in failed] == ["TOTAL_ARITHMETIC"]
    assert failed[0].message == "subtotal + tax = total: expected 135.64, printed 235.64"
    consistency = consistency_by_field(checks)
    assert consistency["total"] is False  # only check it is in failed
    assert consistency["subtotal"] is True  # corroborated by the line sum
    assert "invoice_number" not in consistency  # no check involves it


def test_line_and_date_checks() -> None:
    rows = [{"quantity": "3", "unit_price": "2.00", "amount": "7.00"}]
    checks = run_checks(
        {"invoice_date": "2026-03-14", "due_date": "2026-04-30", "payment_terms_days": 30},
        rows,
        table="line_items",
    )
    status = {c.code: c.status for c in checks}
    assert status == {"LINE_AMOUNT": CheckStatus.FAIL, "DUE_DATE_TERMS": CheckStatus.FAIL}
    order = run_checks({"invoice_date": "2026-03-14", "due_date": "2026-03-01"}, [], table=None)
    assert [(c.code, c.status) for c in order] == [("DUE_AFTER_ISSUE", CheckStatus.FAIL)]


def test_tolerance_absorbs_rounding() -> None:
    rows = [{"quantity": "3", "unit_price": "0.333", "amount": "1.00"}]
    (check,) = run_checks({}, rows, table="line_items", tolerance=Decimal("0.01"))
    assert check.status == CheckStatus.PASS


def test_bank_statement_reconciliation() -> None:
    scalars = {"opening_balance": "1000.00", "closing_balance": "1150.00"}
    rows = [
        {"credit": "500.00", "debit": None},
        {"credit": None, "debit": "350.00"},
    ]
    (check,) = run_checks(scalars, rows, table="transactions")
    assert (check.code, check.status) == ("BALANCE_RECONCILES", CheckStatus.PASS)
    totals = {**scalars, "total_credits": "500", "total_debits": "300"}
    (check,) = run_checks(totals, [], table="transactions")
    assert check.status == CheckStatus.FAIL


# ------------------------------------------------------------------------------ confidence
BASE = {
    "evidence": "VERIFIED",
    "evidence_score": 100.0,
    "normalization": "OK",
    "ocr": None,
    "anchor": 1.0,
    "conflicts": 0,
    "page_matches_citation": True,
    "consistency": None,
    "agreement": None,
}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, 0.9),  # single source, nothing to cross-check
        ({"agreement": True}, 1.0),
        ({"agreement": False}, 0.6),
        ({"consistency": True, "agreement": True}, 1.0),
        ({"consistency": False}, 0.54),
        ({"evidence": "FUZZY", "evidence_score": 90.0}, 0.81),
        ({"evidence": "UNSUPPORTED"}, 0.09),
        ({"evidence": "NOT_FOUND"}, 0.0),
        ({"normalization": "UNCERTAIN"}, 0.54),
        ({"normalization": "INVALID"}, 0.0),
        ({"ocr": 80.0}, 0.81),
        ({"anchor": 0.75}, 0.675),
        ({"conflicts": 2}, 0.72),
        ({"page_matches_citation": False}, 0.855),
        ({"human": True, "evidence": "NOT_FOUND"}, 1.0),
    ],
)
def test_field_confidence_factors(changes: dict[str, object], expected: float) -> None:
    assert field_confidence({**BASE, **changes}) == pytest.approx(expected)


def test_ocr_factor_and_document_aggregation() -> None:
    assert ocr_factor(None) == 1.0
    assert ocr_factor(100) == 1.0
    assert ocr_factor(0) == 0.5
    assert document_confidence([0.9, 0.95]) == 0.9
    assert document_confidence([0.9, None]) == 0.0  # a missing required field
    assert document_confidence([]) == 1.0


def test_review_routing() -> None:
    thresholds = Thresholds(high=0.85, medium=0.6)
    assert review_level(0.9, failed_checks=False, thresholds=thresholds) == ReviewLevel.AUTO
    assert review_level(0.9, failed_checks=True, thresholds=thresholds) == (
        ReviewLevel.ANALYST_REVIEW
    )
    assert review_level(0.7, failed_checks=False, thresholds=thresholds) == (
        ReviewLevel.ANALYST_REVIEW
    )
    assert review_level(0.5, failed_checks=False, thresholds=thresholds) == (
        ReviewLevel.MANDATORY_REVIEW
    )

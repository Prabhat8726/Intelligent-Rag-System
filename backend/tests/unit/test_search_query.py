"""Natural-language search requests to filters (Module 28)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from docintel.db.models import DocumentType
from docintel.search.query import Comparison, parse_query
from docintel.search.service import terms_in_text


def test_master_prompt_examples() -> None:
    invoices = parse_query("Find all invoices from Vendor X")
    assert invoices.document_types == (DocumentType.INVOICE,)
    assert invoices.vendor == "X"
    assert invoices.text == ""

    contracts = parse_query("contracts containing termination clauses")
    assert contracts.document_types == (DocumentType.CONTRACT,)
    assert contracts.vendor is None
    assert contracts.text == "termination clauses"

    terms = parse_query("Documents mentioning payment terms longer than 60 days")
    assert terms.document_types == ()
    assert terms.payment_terms_days == Comparison("gt", Decimal(60))
    assert terms.text == ""
    assert terms.recognized == ("payment terms more than 60 days",)


@pytest.mark.parametrize(
    ("query", "op", "days"),
    [
        ("payment terms of at least 45 days", "gte", 45),
        ("payment terms shorter than 30 days", "lt", 30),
        ("payment terms up to 30 days", "lte", 30),
        ("payment terms of 60 days", "eq", 60),
        ("invoices with net 90", "eq", 90),
    ],
)
def test_payment_terms(query: str, op: str, days: int) -> None:
    assert parse_query(query).payment_terms_days == Comparison(op, Decimal(days))


def test_amounts_dates_and_vendors() -> None:
    parsed = parse_query('purchase orders from "Acme & Sons" over $10,000 in March 2026')
    assert parsed.document_types == (DocumentType.PURCHASE_ORDER,)
    assert parsed.vendor == "Acme & Sons"
    assert parsed.total == Comparison("gt", Decimal(10000))
    assert (parsed.date_from, parsed.date_to) == (date(2026, 3, 1), date(2026, 3, 31))

    parsed = parse_query("bills from supplier Northwind Traders between 2026-01-01 and 2026-03-31")
    assert parsed.document_types == (DocumentType.INVOICE,)
    assert parsed.vendor == "Northwind Traders"
    assert (parsed.date_from, parsed.date_to) == (date(2026, 1, 1), date(2026, 3, 31))

    parsed = parse_query("invoices under 2.5k after 2026-06-30 before 2026-08-01")
    assert parsed.total == Comparison("lt", Decimal(2500))
    assert (parsed.date_from, parsed.date_to) == (date(2026, 7, 1), date(2026, 7, 31))

    parsed = parse_query("delivery notes from Kestrel Industrial Supply in 2026")
    assert parsed.document_types == (DocumentType.DELIVERY_NOTE,)
    assert parsed.vendor == "Kestrel Industrial Supply"
    assert (parsed.date_from, parsed.date_to) == (date(2026, 1, 1), date(2026, 12, 31))


def test_unrecognized_words_stay_free_text() -> None:
    parsed = parse_query("show me delivery notes for PO-2026-38140")
    assert parsed.document_types == (DocumentType.DELIVERY_NOTE,)
    assert parsed.text == "PO-2026-38140"
    assert not parse_query("warranty period").has_filters
    assert parse_query("warranty period").text == "warranty period"
    # "30 days" without "payment terms" is not an amount or a terms filter.
    loose = parse_query("invoices more than 30 days")
    assert loose.total is None
    assert loose.payment_terms_days is None
    assert loose.text == "more than 30 days"


def test_comparisons() -> None:
    assert Comparison("gt", Decimal(60)).matches(Decimal(61))
    assert not Comparison("gt", Decimal(60)).matches(Decimal(60))
    assert Comparison("lte", Decimal(30)).matches(Decimal(30))
    assert Comparison("eq", Decimal(45)).matches(Decimal(45))
    assert Comparison("gte", Decimal(10000)).describe() == "at least 10,000"


def test_payment_terms_in_text() -> None:
    text = (
        "Payment Terms: Net 45. Please remit payment within 30 days quoting the invoice number. "
        "Delivery takes 10 days."
    )
    assert [days for days, _ in terms_in_text(text)] == [45, 30]
    assert (
        terms_in_text("The customer shall pay all amounts within 75 days of receipt.")[0][0] == 75
    )
    assert terms_in_text("Delivery within 10 days.") == []

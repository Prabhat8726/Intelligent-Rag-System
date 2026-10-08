"""Normalization of printed values (Module 8)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from docintel.fields.normalize import (
    DateOrder,
    NormalizationContext,
    NormalizationStatus,
    detect_currency,
    find_dates,
    find_numbers,
    infer_date_order,
    name_similarity,
    normalize_label,
    normalize_value,
    organization_key,
    parse_days,
    value_in_source,
)
from docintel.fields.schemas import ValueType

V = ValueType


@pytest.mark.parametrize(
    ("text", "value", "currency"),
    [
        ("$ 3,968.85", "3968.85", "USD"),
        ("1.234,56 €", "1234.56", "EUR"),
        ("£ 584.88", "584.88", "GBP"),
        ("Rs. 12,345.00", "12345", "INR"),
        ("12 345,67 EUR", "12345.67", "EUR"),
        ("(1,234.50)", "-1234.5", None),
        ("1,234.50-", "-1234.5", None),
        ("USD 99", "99", "USD"),
        ("Sales tax (8.25%) $ 327.43", "327.43", "USD"),
    ],
)
def test_amounts(text: str, value: str, currency: str | None) -> None:
    result = normalize_value(V.MONEY, text)
    assert result.status == NormalizationStatus.OK
    assert result.value == value
    assert result.detail["currency"] == currency


def test_ambiguous_digit_grouping_is_reported_and_resolved_by_the_document() -> None:
    assert normalize_value(V.MONEY, "1,234").status == NormalizationStatus.UNCERTAIN
    us = normalize_value(V.MONEY, "1,234", NormalizationContext(decimal_comma=False))
    assert (us.value, us.status) == ("1234", NormalizationStatus.OK)
    eu = normalize_value(V.MONEY, "1,234", NormalizationContext(decimal_comma=True))
    assert (eu.value, eu.status) == ("1.234", NormalizationStatus.OK)


def test_numbers_in_codes_and_cells_are_kept_apart() -> None:
    values = [n.value for n in find_numbers("GSK-150A gasket 10 pcs 6.81 68.10")]
    assert values == [Decimal(150), Decimal(10), Decimal("6.81"), Decimal("68.10")]


def test_currency_symbols_and_codes() -> None:
    assert detect_currency("$ 10") == ("USD", False)  # "$" alone is an assumption
    assert detect_currency("$ 10", context_currency="CAD") == ("CAD", True)
    assert detect_currency("CAD 10") == ("CAD", True)
    assert detect_currency("Mrs. Smith") is None  # "rs." inside a word is not rupees
    assert normalize_value(V.CURRENCY, "$").status == NormalizationStatus.UNCERTAIN
    assert normalize_value(V.CURRENCY, "€").value == "EUR"


@pytest.mark.parametrize(
    ("text", "iso", "rule"),
    [
        ("05/26/2026", "2026-05-26", "only one valid reading"),
        ("30/04/2026", "2026-04-30", "only one valid reading"),
        ("01.03.2026", "2026-03-01", "dotted dates are day-first"),
        ("2026-03-05", "2026-03-05", "year-month-day"),
        ("5 March 2026", "2026-03-05", "month name"),
        ("March 5, 2026", "2026-03-05", "month name"),
        ("05-Mar-26", "2026-03-05", "month name"),
        ("3. März 2026", "2026-03-03", "month name"),
    ],
)
def test_unambiguous_dates(text: str, iso: str, rule: str) -> None:
    result = normalize_value(V.DATE, text)
    assert (result.value, result.status, result.detail["rule"]) == (
        iso,
        NormalizationStatus.OK,
        rule,
    )


def test_ambiguous_dates_are_uncertain_until_the_document_decides() -> None:
    result = normalize_value(V.DATE, "03/04/2026")
    assert result.status == NormalizationStatus.UNCERTAIN
    assert result.value is None
    assert result.detail["alternatives"] == ["2026-04-03", "2026-03-04"]

    order, reason = infer_date_order(["Invoice 03/04/2026", "Due 13/05/2026"], currency="USD")
    assert order == DateOrder.DMY  # an impossible month beats the currency hint
    assert reason is not None
    assert "13/05/2026" in reason
    context = NormalizationContext(date_order=order, date_order_reason=reason)
    assert normalize_value(V.DATE, "03/04/2026", context).value == "2026-04-03"

    assert infer_date_order(["03/04/2026"], "USD")[0] == DateOrder.MDY
    assert infer_date_order(["03/04/2026"], "GBP")[0] == DateOrder.DMY
    assert infer_date_order(["03/04/2026"], None) == (None, None)
    assert infer_date_order(["13/01/2026", "01/13/2026"], "USD") == (None, None)  # conflict


def test_dates_inside_longer_text() -> None:
    text = "Statement period 01/03/2026 - 31/03/2026"
    readings = find_dates(text)
    assert readings[0].ambiguous  # alone, 01/03 could be either order...
    order, reason = infer_date_order([text], None)  # ...but 31/03 settles it for the page
    readings = find_dates(text, NormalizationContext(date_order=order, date_order_reason=reason))
    assert [r.value.isoformat() for r in readings if r.value] == ["2026-03-01", "2026-03-31"]
    assert normalize_value(V.DATE, "Net 30 days").status == NormalizationStatus.INVALID


@pytest.mark.parametrize(
    ("text", "days"),
    [
        ("Net 30 days", 30),
        ("Net 45", 45),
        ("30 Tage", 30),
        ("three months' notice", 90),
        ("2 weeks", 14),
        ("Due on receipt", 0),
    ],
)
def test_payment_terms_and_periods(text: str, days: int) -> None:
    parsed = parse_days(text)
    assert parsed is not None
    assert parsed[0] == days


def test_percent_identifier_quantity_contact_and_boolean() -> None:
    assert normalize_value(V.PERCENT, "VAT (20%)").value == "0.2"
    assert normalize_value(V.PERCENT, "8.25 %").value == "0.0825"
    assert normalize_value(V.IDENTIFIER, " GB 284 7712 09 ").value == "GB284771209"
    assert normalize_value(V.QUANTITY, "2.5").value == "2.5"
    assert normalize_value(V.INTEGER, "No. 7").value == 7
    assert normalize_value(V.EMAIL, "Mail: A.B@Example.com").value == "a.b@example.com"
    assert normalize_value(V.PHONE, "+44 (0)20 7946 0018").value == "+4402079460018"
    assert normalize_value(V.PHONE, "12").status == NormalizationStatus.INVALID
    assert normalize_value(V.BOOLEAN, "shall not renew automatically").value is False
    assert normalize_value(V.BOOLEAN, "renews automatically for one year").value is True
    assert normalize_value(V.BOOLEAN, "see clause 9").status == NormalizationStatus.UNCERTAIN
    assert normalize_value(V.TEXT, "  ").status == NormalizationStatus.INVALID


@pytest.mark.parametrize(
    ("printed", "canonical"),
    [
        ("Kestrel Industrial Supply, Inc", "Kestrel Industrial Supply Inc."),
        ("KESTREL INDUSTRIAL SUPPLY", "Kestrel Industrial Supply Inc."),
        ("Kestrel Ind. Supply Inc.", "Kestrel Industrial Supply Inc."),
        ("Altamira Components G.m.b.H.", "Altamira Components GmbH"),
        ("Harbor and Pine Packaging Ltd", "Harbor & Pine Packaging Ltd."),
        ("HARBOR & PINE PACKAGING LIMITED", "Harbor & Pine Packaging Ltd."),
        ("Bluepeak Office Solutions, L.L.C.", "Bluepeak Office Solutions LLC"),
        ("Sundaram Precision Tools Pvt Ltd", "Sundaram Precision Tools Pvt. Ltd."),
    ],
)
def test_vendor_name_variants_normalize_close_to_the_canonical_name(
    printed: str, canonical: str
) -> None:
    assert name_similarity(organization_key(printed), organization_key(canonical)) >= 85


def test_different_vendors_stay_apart() -> None:
    a = organization_key("Bluepeak Office Solutions LLC")
    b = organization_key("Kestrel Industrial Supply Inc.")
    assert name_similarity(a, b) < 60


def test_labels_normalize_to_one_form() -> None:
    assert normalize_label("Invoice No.:") == "invoice no"
    assert normalize_label("Invoice #") == "invoice no"
    assert normalize_label("Invoice Number") == "invoice no"
    assert normalize_label("#") == "no"


def test_value_must_be_readable_from_its_quote() -> None:
    context = NormalizationContext()
    assert value_in_source(V.MONEY, "4,296.28", "Total Due $ 4,296.28", context)
    assert value_in_source(V.MONEY, "4296.28", "Total Due $ 4,296.28", context)
    assert not value_in_source(V.MONEY, "4,269.28", "Total Due $ 4,296.28", context)
    assert value_in_source(V.DATE, "2026-05-26", "Invoice Date 05/26/2026", context)
    assert not value_in_source(V.DATE, "2026-05-27", "Invoice Date 05/26/2026", context)
    assert value_in_source(V.IDENTIFIER, "INV-1001", "Invoice No. INV-1001", context)
    assert not value_in_source(V.IDENTIFIER, "INV-9999", "Invoice No. INV-1001", context)
    assert not value_in_source(V.TEXT, "anything", "", context)

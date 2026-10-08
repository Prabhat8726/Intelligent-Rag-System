"""Scenario bundles: a purchase order, its delivery note and invoice(s), with controlled defects."""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from docintel.synthetic.catalog import BUYER_NAME, LOCALES, VENDORS, Item, Vendor
from docintel.synthetic.model import (
    DocumentTruth,
    LineItem,
    Rendering,
    SyntheticDocType,
    money,
)

TEMPLATES = ("classic", "modern", "compact")
ALTERNATE_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%B %d, %Y")
_BASE_DATE = date(2026, 1, 5)


class Scenario(StrEnum):
    CLEAN_MATCH = "CLEAN_MATCH"
    UNIT_PRICE_MISMATCH = "UNIT_PRICE_MISMATCH"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    SHORT_DELIVERY = "SHORT_DELIVERY"
    MISSING_PO_REFERENCE = "MISSING_PO_REFERENCE"
    TOTAL_ARITHMETIC_ERROR = "TOTAL_ARITHMETIC_ERROR"
    TAX_RATE_MISMATCH = "TAX_RATE_MISMATCH"
    VENDOR_MISMATCH = "VENDOR_MISMATCH"
    VENDOR_NAME_VARIANT = "VENDOR_NAME_VARIANT"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    LONG_MULTIPAGE = "LONG_MULTIPAGE"
    SCANNED_DOCUMENTS = "SCANNED_DOCUMENTS"


@dataclass(slots=True)
class _Base:
    vendor: Vendor
    bundle_id: str
    po_number: str
    po_date: date
    delivery_date: date
    invoice_date: date
    lines: list[LineItem]


def _pick_items(vendor: Vendor, count: int, rng: random.Random) -> list[Item]:
    """Distinct SKUs per document (real POs don't repeat a SKU at different prices).

    Documents longer than the catalog get lot variants with their own SKUs.
    """
    if count <= len(vendor.items):
        return rng.sample(vendor.items, count)
    catalog = len(vendor.items)
    return [
        Item(
            sku=f"{base.sku}-L{index // catalog + 1:02d}",
            description=f"{base.description} (lot {index // catalog + 1})",
            unit=base.unit,
            base_price=base.base_price,
        )
        for index, base in ((i, vendor.items[i % catalog]) for i in range(count))
    ]


def _base(scenario: Scenario, index: int, rng: random.Random) -> _Base:
    vendor = rng.choice(VENDORS)
    line_count = 45 if scenario == Scenario.LONG_MULTIPAGE else rng.randint(2, 4)
    items = _pick_items(vendor, line_count, rng)
    lines = [
        LineItem(
            line_number=number,
            sku=item.sku,
            description=item.description,
            quantity=Decimal(rng.choice((1, 2, 4, 5, 10, 12, 20, 25, 40, 50, 100, 150))),
            unit=item.unit,
            unit_price=money(item.base_price * Decimal(str(round(rng.uniform(0.94, 1.12), 3)))),
        )
        for number, item in enumerate(items, start=1)
    ]
    po_date = _BASE_DATE + timedelta(days=rng.randint(0, 240))
    delivery_date = po_date + timedelta(days=rng.randint(3, 14))
    return _Base(
        vendor=vendor,
        bundle_id=f"B{index:04d}",
        po_number=f"PO-2026-{rng.randint(10000, 99999)}",
        po_date=po_date,
        delivery_date=delivery_date,
        invoice_date=delivery_date + timedelta(days=rng.randint(0, 5)),
        lines=lines,
    )


def _rendering(rng: random.Random, vendor: Vendor) -> Rendering:
    return Rendering(
        template=rng.choice(TEMPLATES), date_format=LOCALES[vendor.country].date_format
    )


def _purchase_order(base: _Base, scenario: Scenario, rng: random.Random) -> DocumentTruth:
    locale = LOCALES[base.vendor.country]
    truth = DocumentTruth(
        doc_id=f"{base.bundle_id}-PO",
        bundle_id=base.bundle_id,
        scenario=scenario.value,
        document_type=SyntheticDocType.PURCHASE_ORDER,
        number=base.po_number,
        vendor_code=base.vendor.code,
        vendor_name_printed=base.vendor.name,
        vendor_name_canonical=base.vendor.name,
        buyer_name=BUYER_NAME,
        issue_date=base.po_date,
        currency=locale.currency,
        line_items=copy.deepcopy(base.lines),
        rendering=_rendering(rng, base.vendor),
        payment_terms_days=base.vendor.payment_terms_days,
        tax_label=locale.tax_label,
        tax_rate=locale.tax_rate,
    )
    _price(truth, locale.tax_rate)
    return truth


def _delivery_note(base: _Base, scenario: Scenario, rng: random.Random) -> DocumentTruth:
    return DocumentTruth(
        doc_id=f"{base.bundle_id}-DN",
        bundle_id=base.bundle_id,
        scenario=scenario.value,
        document_type=SyntheticDocType.DELIVERY_NOTE,
        number=f"DN-{base.vendor.code}-{rng.randint(100000, 999999)}",
        vendor_code=base.vendor.code,
        vendor_name_printed=base.vendor.name,
        vendor_name_canonical=base.vendor.name,
        buyer_name=BUYER_NAME,
        issue_date=base.delivery_date,
        currency=LOCALES[base.vendor.country].currency,
        line_items=copy.deepcopy(base.lines),
        rendering=_rendering(rng, base.vendor),
        purchase_order_number=base.po_number,
    )


def _invoice(
    base: _Base, scenario: Scenario, rng: random.Random, suffix: str = "INV"
) -> DocumentTruth:
    locale = LOCALES[base.vendor.country]
    truth = DocumentTruth(
        doc_id=f"{base.bundle_id}-{suffix}",
        bundle_id=base.bundle_id,
        scenario=scenario.value,
        document_type=SyntheticDocType.INVOICE,
        number=f"INV-{base.vendor.code}-{rng.randint(1000000, 9999999)}",
        vendor_code=base.vendor.code,
        vendor_name_printed=base.vendor.name,
        vendor_name_canonical=base.vendor.name,
        buyer_name=BUYER_NAME,
        issue_date=base.invoice_date,
        currency=locale.currency,
        line_items=copy.deepcopy(base.lines),
        rendering=_rendering(rng, base.vendor),
        purchase_order_number=base.po_number,
        due_date=base.invoice_date + timedelta(days=base.vendor.payment_terms_days),
        payment_terms_days=base.vendor.payment_terms_days,
        tax_label=locale.tax_label,
        tax_rate=locale.tax_rate,
    )
    _price(truth, locale.tax_rate)
    return truth


def _price(truth: DocumentTruth, tax_rate: Decimal) -> None:
    """Compute consistent printed totals from the line items."""
    subtotal = truth.computed_subtotal
    tax = money(subtotal * tax_rate)
    truth.printed_subtotal = subtotal
    truth.printed_tax = tax
    truth.printed_total = subtotal + tax


def build_bundle(scenario: Scenario, index: int, rng: random.Random) -> list[DocumentTruth]:
    base = _base(scenario, index, rng)
    po = _purchase_order(base, scenario, rng)
    dn = _delivery_note(base, scenario, rng)
    invoice = _invoice(base, scenario, rng)
    locale = LOCALES[base.vendor.country]
    bundle = [po, dn, invoice]

    match scenario:
        case Scenario.CLEAN_MATCH | Scenario.LONG_MULTIPAGE:
            pass
        case Scenario.UNIT_PRICE_MISMATCH:
            line = rng.choice(invoice.line_items)
            expected = line.unit_price
            line.unit_price = money(
                expected * (1 + Decimal(rng.choice(("0.0625", "0.08", "0.12"))))
            )
            _price(invoice, locale.tax_rate)
            invoice.defects.append(
                {
                    "code": "UNIT_PRICE_MISMATCH",
                    "line_number": line.line_number,
                    "sku": line.sku,
                    "compared_with": po.doc_id,
                    "expected": str(expected),
                    "actual": str(line.unit_price),
                }
            )
        case Scenario.QUANTITY_MISMATCH:
            line = rng.choice(invoice.line_items)
            expected = line.quantity
            line.quantity = expected + Decimal(rng.choice((1, 2, 5, 10)))
            _price(invoice, locale.tax_rate)
            invoice.defects.append(
                {
                    "code": "QUANTITY_MISMATCH",
                    "line_number": line.line_number,
                    "sku": line.sku,
                    "compared_with": po.doc_id,
                    "expected": str(expected),
                    "actual": str(line.quantity),
                }
            )
        case Scenario.SHORT_DELIVERY:
            line = rng.choice([item for item in dn.line_items if item.quantity > 1])
            ordered = line.quantity
            line.quantity = max(Decimal(1), (ordered * Decimal("0.6")).to_integral_value())
            invoice.defects.append(
                {
                    "code": "BILLED_QUANTITY_EXCEEDS_DELIVERED",
                    "line_number": line.line_number,
                    "sku": line.sku,
                    "compared_with": dn.doc_id,
                    "expected": str(line.quantity),
                    "actual": str(ordered),
                }
            )
        case Scenario.MISSING_PO_REFERENCE:
            invoice.purchase_order_number = None
            invoice.defects.append({"code": "MISSING_PO_REFERENCE"})
        case Scenario.TOTAL_ARITHMETIC_ERROR:
            correct = invoice.printed_total or invoice.computed_subtotal
            invoice.printed_total = correct + Decimal(rng.choice(("100.00", "9.00", "0.90")))
            invoice.defects.append(
                {
                    "code": "TOTAL_MISMATCH",
                    "expected": str(correct),
                    "actual": str(invoice.printed_total),
                }
            )
        case Scenario.TAX_RATE_MISMATCH:
            wrong_rate = locale.tax_rate + Decimal("0.02")
            invoice.tax_rate = wrong_rate
            _price(invoice, wrong_rate)
            invoice.defects.append(
                {
                    "code": "TAX_RATE_MISMATCH",
                    "expected": str(locale.tax_rate),
                    "actual": str(wrong_rate),
                }
            )
        case Scenario.VENDOR_MISMATCH:
            other = rng.choice([vendor for vendor in VENDORS if vendor.code != base.vendor.code])
            invoice.vendor_code = other.code
            invoice.vendor_name_printed = other.name
            invoice.vendor_name_canonical = other.name
            invoice.defects.append(
                {"code": "VENDOR_MISMATCH", "expected": base.vendor.name, "actual": other.name}
            )
        case Scenario.VENDOR_NAME_VARIANT:
            invoice.vendor_name_printed = rng.choice(base.vendor.name_variants)
            invoice.variations.append(
                {"code": "VENDOR_NAME_VARIANT", "printed": invoice.vendor_name_printed}
            )
            invoice.rendering.date_format = rng.choice(ALTERNATE_DATE_FORMATS)
            invoice.variations.append(
                {"code": "DATE_FORMAT_VARIANT", "format": invoice.rendering.date_format}
            )
        case Scenario.DUPLICATE_INVOICE:
            resent = copy.deepcopy(invoice)
            resent.doc_id = f"{base.bundle_id}-INV2"
            resent.rendering = Rendering(
                template=rng.choice([t for t in TEMPLATES if t != invoice.rendering.template]),
                date_format=rng.choice(ALTERNATE_DATE_FORMATS),
            )
            resent.defects.append({"code": "DUPLICATE_INVOICE", "duplicate_of": invoice.doc_id})
            bundle.append(resent)
        case Scenario.SCANNED_DOCUMENTS:
            invoice.rendering.variant = "scanned"
            invoice.rendering.file_format = "pdf"
            invoice.rendering.scan_profile = rng.choice(("light", "heavy"))
            dn.rendering.variant = "scanned"
            dn.rendering.file_format = rng.choice(("png", "tiff"))
            dn.rendering.scan_profile = "light"
    return bundle

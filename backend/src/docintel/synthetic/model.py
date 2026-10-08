"""Ground-truth model for synthetic documents.

`defects` are discrepancies the platform must detect (evaluation recall).
`variations` are formatting differences it must NOT report as discrepancies once normalized
(evaluation precision) - e.g. a vendor name spelled differently or another date format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class SyntheticDocType(StrEnum):
    PURCHASE_ORDER = "PURCHASE_ORDER"
    INVOICE = "INVOICE"
    DELIVERY_NOTE = "DELIVERY_NOTE"


@dataclass(slots=True)
class LineItem:
    line_number: int
    sku: str
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal

    @property
    def line_total(self) -> Decimal:
        return money(self.quantity * self.unit_price)

    def to_json(self, *, with_prices: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "line_number": self.line_number,
            "sku": self.sku,
            "description": self.description,
            "quantity": str(self.quantity),
            "unit": self.unit,
        }
        if with_prices:
            data["unit_price"] = str(self.unit_price)
            data["line_total"] = str(self.line_total)
        return data


@dataclass(slots=True)
class Rendering:
    template: str
    date_format: str
    variant: str = "native"  # native | scanned
    file_format: str = "pdf"  # pdf | png | tiff
    scan_profile: str | None = None
    dpi: int | None = None


@dataclass(slots=True)
class DocumentTruth:
    doc_id: str
    bundle_id: str
    scenario: str
    document_type: SyntheticDocType
    number: str
    vendor_code: str
    vendor_name_printed: str
    vendor_name_canonical: str
    buyer_name: str
    issue_date: date
    currency: str
    line_items: list[LineItem]
    rendering: Rendering
    purchase_order_number: str | None = None
    due_date: date | None = None
    payment_terms_days: int | None = None
    tax_label: str | None = None
    tax_rate: Decimal | None = None
    printed_subtotal: Decimal | None = None
    printed_tax: Decimal | None = None
    printed_total: Decimal | None = None
    defects: list[dict[str, Any]] = field(default_factory=list)
    variations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_prices(self) -> bool:
        return self.document_type != SyntheticDocType.DELIVERY_NOTE

    @property
    def computed_subtotal(self) -> Decimal:
        return money(sum((item.line_total for item in self.line_items), Decimal(0)))

    def to_json(self) -> dict[str, Any]:
        def opt(value: object) -> str | None:
            return None if value is None else str(value)

        return {
            "doc_id": self.doc_id,
            "bundle_id": self.bundle_id,
            "scenario": self.scenario,
            "document_type": self.document_type.value,
            "fields": {
                "number": self.number,
                "vendor_name": self.vendor_name_printed,
                "vendor_name_canonical": self.vendor_name_canonical,
                "vendor_code": self.vendor_code,
                "buyer_name": self.buyer_name,
                "issue_date": self.issue_date.isoformat(),
                "due_date": opt(self.due_date and self.due_date.isoformat()),
                "payment_terms_days": self.payment_terms_days,
                "currency": self.currency,
                "purchase_order_number": self.purchase_order_number,
                "tax_label": self.tax_label,
                "tax_rate": opt(self.tax_rate),
                "subtotal": opt(self.printed_subtotal),
                "tax": opt(self.printed_tax),
                "total": opt(self.printed_total),
            },
            "line_items": [item.to_json(with_prices=self.has_prices) for item in self.line_items],
            "defects": self.defects,
            "variations": self.variations,
            "rendering": {
                "template": self.rendering.template,
                "date_format": self.rendering.date_format,
                "variant": self.rendering.variant,
                "file_format": self.rendering.file_format,
                "scan_profile": self.rendering.scan_profile,
                "dpi": self.rendering.dpi,
            },
        }

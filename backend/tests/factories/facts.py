"""Hand-built DocumentFacts for matching and rule tests (no extraction involved)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from docintel.db.models import DocumentType
from docintel.fields.schemas import ValueType
from docintel.matching.facts import (
    DATE_FIELD,
    NUMBER_FIELD,
    PO_REFERENCE_FIELD,
    VENDOR_FIELD,
    DocumentFacts,
    FactValue,
    LineFacts,
)

_TYPES: dict[str, ValueType] = {
    "total": ValueType.MONEY,
    "subtotal": ValueType.MONEY,
    "tax_amount": ValueType.MONEY,
    "unit_price": ValueType.MONEY,
    "amount": ValueType.MONEY,
    "contract_value": ValueType.MONEY,
    "quantity": ValueType.QUANTITY,
    "tax_rate": ValueType.PERCENT,
    "payment_terms_days": ValueType.DAYS,
    "currency": ValueType.CURRENCY,
    "auto_renewal": ValueType.BOOLEAN,
}
KESTREL_ID = str(uuid.UUID(int=1))
_EPOCH = datetime(2026, 3, 1, tzinfo=UTC)


def fact(
    name: str,
    value: Any,
    *,
    path: str | None = None,
    confidence: float = 0.98,
    page: int = 1,
    display: str | None = None,
    corrected: bool = False,
    ambiguous: bool = False,
) -> FactValue:
    value_type = _TYPES.get(name, ValueType.DATE if name.endswith("date") else ValueType.TEXT)
    if value_type in {ValueType.MONEY, ValueType.QUANTITY, ValueType.PERCENT}:
        typed: Any = Decimal(str(value))
    elif value_type == ValueType.DATE:
        typed = date.fromisoformat(str(value))
    elif value_type == ValueType.DAYS:
        typed = int(value)
    else:
        typed = value
    return FactValue(
        path=path or name,
        name=name,
        value_type=value_type,
        value=typed,
        display=display if display is not None else str(value),
        page=page,
        source_text=f"{name} {value}",
        bbox=[10.0, 10.0, 50.0, 20.0],
        confidence=confidence,
        evidence="VERIFIED",
        corrected=corrected,
        ambiguous=ambiguous,
        field_id=str(uuid.uuid4()),
    )


def line(
    index: int,
    sku: str | None,
    quantity: str | int,
    unit_price: str | None = None,
    *,
    description: str | None = None,
    confidence: float = 0.98,
) -> LineFacts:
    cells: dict[str, FactValue] = {}
    values: dict[str, Any] = {
        "sku": sku,
        "description": description or (f"Item {sku}" if sku else None),
        "quantity": quantity,
        "unit": "pcs",
        "unit_price": unit_price,
    }
    if unit_price is not None:
        values["amount"] = Decimal(str(quantity)) * Decimal(unit_price)
    for name, value in values.items():
        if value is not None:
            cells[name] = fact(
                name, value, path=f"line_items[{index}].{name}", confidence=confidence
            )
    return LineFacts(index, cells)


def document(
    document_type: DocumentType,
    *,
    number: str,
    po: str | None = None,
    vendor: str | None = "Kestrel Industrial Supply Inc.",
    vendor_id: str | None = KESTREL_ID,
    issued: str = "2026-03-14",
    currency: str | None = "USD",
    total: str | None = None,
    lines: Sequence[LineFacts] = (),
    created_minutes: int = 0,
    fields: dict[str, Any] | None = None,
    checks: Sequence[dict[str, Any]] = (),
) -> DocumentFacts:
    scalars: dict[str, FactValue] = {}

    def put(name: str | None, value: Any) -> None:
        if name and value is not None:
            scalars[name] = fact(name, value)

    put(NUMBER_FIELD.get(document_type), number)
    put(DATE_FIELD.get(document_type), issued)
    put(VENDOR_FIELD.get(document_type), vendor)
    if document_type != DocumentType.PURCHASE_ORDER:
        put(PO_REFERENCE_FIELD, po)
    put("currency", currency)
    put("total", total)
    for name, value in (fields or {}).items():
        put(name, value)
    return DocumentFacts(
        document_type=document_type,
        fields=scalars,
        lines=list(lines),
        document_id=uuid.uuid4(),
        label=f"{number}.pdf",
        created_at=_EPOCH + timedelta(minutes=created_minutes),
        vendor_id=vendor_id,
        vendor_name=vendor if vendor_id else None,
        failed_checks=[dict(check) for check in checks],
    )


def purchase_order(lines: Sequence[LineFacts], **kwargs: Any) -> DocumentFacts:
    kwargs.setdefault("number", "PO-2026-10001")
    kwargs.setdefault("fields", {"tax_rate": "0.0825", "payment_terms_days": 30})
    return document(DocumentType.PURCHASE_ORDER, lines=lines, **kwargs)


def invoice(lines: Sequence[LineFacts], **kwargs: Any) -> DocumentFacts:
    kwargs.setdefault("number", "INV-2026-0042")
    kwargs.setdefault("po", "PO-2026-10001")
    kwargs.setdefault("fields", {"tax_rate": "0.0825", "payment_terms_days": 30})
    return document(DocumentType.INVOICE, lines=lines, **kwargs)


def delivery(lines: Sequence[LineFacts], **kwargs: Any) -> DocumentFacts:
    kwargs.setdefault("number", "DN-KIS-100200")
    kwargs.setdefault("po", "PO-2026-10001")
    kwargs.setdefault("currency", None)
    return document(DocumentType.DELIVERY_NOTE, lines=lines, **kwargs)

"""Document facts: the typed, evidence-carrying view of one extraction that matching works on.

Built from `ResolvedField`s, which both a fresh `ExtractionOutcome` (evaluation) and stored
`extracted_fields` rows (worker, API) provide. A reviewer's correction replaces the machine value
and counts as certain.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from docintel.db.models import DocumentType
from docintel.fields.normalize import NormalizationStatus, organization_key
from docintel.fields.schemas import SCHEMA_INFO, ValueType
from docintel.fields.service import ResolvedField

# Own number and PO reference per document type (the fields matching keys on).
NUMBER_FIELD: dict[DocumentType, str] = {
    DocumentType.INVOICE: "invoice_number",
    DocumentType.PURCHASE_ORDER: "po_number",
    DocumentType.DELIVERY_NOTE: "delivery_note_number",
    DocumentType.RECEIPT: "receipt_number",
    DocumentType.CONTRACT: "contract_number",
    DocumentType.POLICY: "policy_number",
}
DATE_FIELD: dict[DocumentType, str] = {
    DocumentType.INVOICE: "invoice_date",
    DocumentType.PURCHASE_ORDER: "po_date",
    DocumentType.DELIVERY_NOTE: "delivery_date",
    DocumentType.RECEIPT: "transaction_date",
    DocumentType.CONTRACT: "effective_date",
    DocumentType.POLICY: "effective_date",
}
VENDOR_FIELD: dict[DocumentType, str] = {
    DocumentType.INVOICE: "vendor_name",
    DocumentType.PURCHASE_ORDER: "vendor_name",
    DocumentType.DELIVERY_NOTE: "vendor_name",
    DocumentType.RECEIPT: "merchant_name",
}
TOTAL_FIELD: dict[DocumentType, str] = {
    DocumentType.INVOICE: "total",
    DocumentType.PURCHASE_ORDER: "total",
    DocumentType.RECEIPT: "total",
    DocumentType.CONTRACT: "contract_value",
}
PO_REFERENCE_FIELD = "purchase_order_number"

_NUMERIC = frozenset({ValueType.MONEY, ValueType.QUANTITY, ValueType.PERCENT})
_INTEGER = frozenset({ValueType.DAYS, ValueType.INTEGER})
_KEY_DROP = re.compile(r"[^0-9A-Z]")


def reference_key(text: str | None) -> str | None:
    """Comparable form of a document number or SKU: 'inv-2026/0042 ' -> 'INV20260042'."""
    if not text:
        return None
    key = _KEY_DROP.sub("", unicodedata.normalize("NFKC", text).upper())
    return key or None


def typed_value(value_type: ValueType, value: Any) -> Any:
    """Normalized JSON value -> Python type used for comparisons (None if unusable)."""
    if value is None or value == "":
        return None
    try:
        if value_type in _NUMERIC:
            return Decimal(str(value))
        if value_type == ValueType.DATE:
            return date.fromisoformat(str(value))
        if value_type in _INTEGER:
            return int(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if value_type == ValueType.BOOLEAN:
        return bool(value)
    return str(value)


@dataclass(frozen=True, slots=True)
class FactValue:
    """One extracted value with what is needed to show where it came from."""

    path: str
    name: str
    value_type: ValueType
    value: Any
    display: str | None  # as printed, or as the reviewer entered it
    page: int | None
    source_text: str | None
    bbox: list[float] | None
    confidence: float
    evidence: str
    corrected: bool = False
    ambiguous: bool = False  # normalization UNCERTAIN (e.g. day/month order)
    field_id: str | None = None

    def uncertain(self, min_confidence: float) -> bool:
        """A machine value too weak to call a difference a real discrepancy."""
        if self.corrected:
            return False
        return self.ambiguous or self.confidence < min_confidence

    def text(self) -> str | None:
        if self.value is None:
            return None
        if isinstance(self.value, Decimal):
            return format(self.value.normalize(), "f")
        if isinstance(self.value, date):
            return self.value.isoformat()
        return str(self.value)


@dataclass(slots=True)
class LineFacts:
    index: int
    cells: dict[str, FactValue] = field(default_factory=dict)

    def get(self, name: str) -> FactValue | None:
        cell = self.cells.get(name)
        return cell if cell is not None and cell.value is not None else None

    def value(self, name: str) -> Any:
        cell = self.get(name)
        return cell.value if cell else None

    @property
    def sku_key(self) -> str | None:
        sku = self.get("sku")
        return reference_key(str(sku.value)) if sku else None

    @property
    def label(self) -> str:
        """How a line is named in explanations: its SKU, else its description, else its number."""
        for name in ("sku", "description"):
            cell = self.get(name)
            if cell is not None:
                return str(cell.value)
        return f"line {self.index + 1}"

    def page(self) -> int | None:
        return next((cell.page for cell in self.cells.values() if cell.page is not None), None)


@dataclass(slots=True)
class DocumentFacts:
    document_type: DocumentType
    fields: dict[str, FactValue]
    lines: list[LineFacts]
    document_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    extraction_id: uuid.UUID | None = None
    label: str = ""
    created_at: datetime | None = None
    vendor_id: str | None = None
    vendor_name: str | None = None
    failed_checks: list[dict[str, Any]] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    review_level: str | None = None  # the extraction's routing (AUTO / ANALYST / MANDATORY)

    def get(self, name: str) -> FactValue | None:
        item = self.fields.get(name)
        return item if item is not None and item.value is not None else None

    def value(self, name: str) -> Any:
        item = self.get(name)
        return item.value if item else None

    @property
    def number(self) -> FactValue | None:
        name = NUMBER_FIELD.get(self.document_type)
        return self.get(name) if name else None

    @property
    def number_key(self) -> str | None:
        number = self.number
        return reference_key(str(number.value)) if number else None

    @property
    def po_reference(self) -> FactValue | None:
        if self.document_type == DocumentType.PURCHASE_ORDER:
            return self.number
        return self.get(PO_REFERENCE_FIELD)

    @property
    def po_key(self) -> str | None:
        reference = self.po_reference
        return reference_key(str(reference.value)) if reference else None

    @property
    def document_date(self) -> date | None:
        name = DATE_FIELD.get(self.document_type)
        value = self.value(name) if name else None
        return value if isinstance(value, date) else None

    @property
    def total(self) -> Decimal | None:
        name = TOTAL_FIELD.get(self.document_type)
        value = self.value(name) if name else None
        return value if isinstance(value, Decimal) else None

    @property
    def currency(self) -> str | None:
        value = self.value("currency")
        return str(value) if value else None

    @property
    def vendor(self) -> FactValue | None:
        name = VENDOR_FIELD.get(self.document_type)
        return self.get(name) if name else None

    @property
    def vendor_key(self) -> str | None:
        """Organization key of the canonical vendor if resolved, else of the printed name."""
        name = self.vendor_name or (str(self.vendor.value) if self.vendor else None)
        return organization_key(name) if name else None

    @property
    def display_name(self) -> str:
        number = self.number
        if number is not None:
            return f"{self.document_type.value.replace('_', ' ').lower()} {number.value}"
        return self.label or self.document_type.value


def _fact(item: ResolvedField, field_id: str | None) -> FactValue:
    source = item.corrected_normalized if item.corrected else item.normalized
    value = typed_value(item.value_type, item.value)
    if item.corrected and item.corrected_value == "":
        value = None  # a reviewer confirmed the value is not on the document
    return FactValue(
        path=item.path,
        name=item.name,
        value_type=item.value_type,
        value=value,
        display=item.display_value,
        page=item.page,
        source_text=item.source_text,
        bbox=item.bbox,
        confidence=1.0 if item.corrected else item.confidence,
        evidence="HUMAN" if item.corrected else item.evidence.value,
        corrected=item.corrected,
        ambiguous=(source or {}).get("status") == NormalizationStatus.UNCERTAIN.value,
        field_id=field_id,
    )


def facts_from_fields(
    document_type: DocumentType,
    fields: Iterable[ResolvedField],
    *,
    field_ids: Mapping[str, str] | None = None,
    checks: Iterable[Mapping[str, Any]] = (),
    **identity: Any,
) -> DocumentFacts:
    """`identity`: document_id, version_id, extraction_id, label, created_at."""
    scalars: dict[str, FactValue] = {}
    rows: dict[int, LineFacts] = {}
    missing: list[str] = []
    vendor_id: str | None = None
    vendor_name: str | None = None
    vendor_field = VENDOR_FIELD.get(document_type)
    schema = SCHEMA_INFO.get(document_type)
    row_count = schema.table.name if schema is not None and schema.table is not None else None
    for item in fields:
        fact = _fact(item, (field_ids or {}).get(item.path))
        if item.group is not None and item.row_index is not None and "[" in item.path:
            if "." in item.path:  # a table cell, not a list item
                rows.setdefault(item.row_index, LineFacts(item.row_index)).cells[item.name] = fact
            continue
        if item.group is not None:
            continue
        scalars[item.name] = fact
        if item.required and fact.value is None and item.name != row_count:
            missing.append(item.name)  # (no table rows is the extraction's own finding)
        if item.name == vendor_field:
            source = item.corrected_normalized if item.corrected else item.normalized
            match = (source or {}).get("vendor")
            if match:
                vendor_id = str(match.get("vendor_id"))
                vendor_name = match.get("canonical_name")
    return DocumentFacts(
        document_type=document_type,
        fields=scalars,
        lines=[rows[index] for index in sorted(rows)],
        vendor_id=vendor_id,
        vendor_name=vendor_name,
        failed_checks=[dict(check) for check in checks if check.get("status") == "FAIL"],
        missing_required=missing,
        **identity,
    )

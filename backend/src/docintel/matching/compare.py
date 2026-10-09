"""Cross-document comparison (Module 9): invoice vs purchase order vs delivery notes.

Every item states a fact with evidence from both sides: MATCH (equal, or within the tolerance
the rules configure), MISMATCH, MISSING (one side has no value or no such line) or UNCERTAIN (the
values differ but one of them is a weak machine reading, or the currencies differ and amounts are
not converted - assumption A5). Business consequences (fail, warn) are the rule engine's job.

Lines are paired by SKU, else by description similarity. Delivered quantities are summed over all
delivery notes for the same order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from rapidfuzz import fuzz

from docintel.db.models import (
    ComparisonCategory,
    ComparisonItemStatus,
    ComparisonRole,
    ComparisonType,
    DocumentType,
)
from docintel.fields.normalize import name_similarity
from docintel.matching.facts import DocumentFacts, FactValue, LineFacts

# The enums are shared with the database (one definition), under the names used here.
Role = ComparisonRole
ItemStatus = ComparisonItemStatus
Category = ComparisonCategory

ROLE_OF: dict[DocumentType, ComparisonRole] = {
    DocumentType.INVOICE: ComparisonRole.INVOICE,
    DocumentType.PURCHASE_ORDER: ComparisonRole.PURCHASE_ORDER,
    DocumentType.DELIVERY_NOTE: ComparisonRole.DELIVERY_NOTE,
}


# Checks (stable keys the rules refer to).
VENDOR = "vendor"
CURRENCY = "currency"
PO_REFERENCE = "po_reference"
TAX_RATE = "tax_rate"
PAYMENT_TERMS = "payment_terms"
UNIT_PRICE = "unit_price"
QUANTITY_ORDERED = "quantity_vs_ordered"
QUANTITY_DELIVERED = "quantity_vs_delivered"
LINE_ON_ORDER = "line_on_order"  # subject line found on the purchase order
LINE_FULFILLED = "line_fulfilled"  # order line found on the subject (invoiced / delivered)
LINE_DELIVERED = "line_delivered"  # invoice line found on a delivery note


@dataclass(frozen=True, slots=True)
class Tolerances:
    """What counts as equal. Built from the configured rules (one source of truth)."""

    price_abs: Decimal = Decimal("0.01")
    price_pct: Decimal = Decimal("0")
    quantity_abs: Decimal = Decimal("0")
    tax_rate_abs: Decimal = Decimal("0.0001")
    min_confidence: float = 0.85  # a difference involving a weaker machine value is UNCERTAIN
    description_similarity: float = 85.0
    vendor_similarity: float = 90.0


@dataclass(frozen=True, slots=True)
class Side:
    """One document's value for an item, with where it was read."""

    role: Role
    document_id: str | None
    document_label: str
    value: str | None
    printed: str | None
    field_id: str | None = None
    page: int | None = None
    source_text: str | None = None
    bbox: list[float] | None = None
    confidence: float | None = None
    corrected: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "document_id": self.document_id,
            "document_label": self.document_label,
            "value": self.value,
            "printed": self.printed,
            "field_id": self.field_id,
            "page": self.page,
            "source_text": self.source_text,
            "bbox": self.bbox,
            "confidence": self.confidence,
            "corrected": self.corrected,
        }


@dataclass(slots=True)
class ComparisonItem:
    key: str
    category: Category
    check: str
    status: ItemStatus
    left: list[Side]
    right: list[Side]
    left_value: str | None
    right_value: str | None
    explanation: str
    line: str | None = None
    difference: dict[str, str] | None = None
    tolerance: dict[str, str] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category.value,
            "check": self.check,
            "status": self.status.value,
            "line": self.line,
            "left_value": self.left_value,
            "right_value": self.right_value,
            "difference": self.difference,
            "tolerance": self.tolerance,
            "explanation": self.explanation,
            "left": [side.to_json() for side in self.left],
            "right": [side.to_json() for side in self.right],
        }


@dataclass(slots=True)
class ComparisonOutcome:
    comparison_type: ComparisonType
    subject: DocumentFacts
    purchase_order: DocumentFacts | None
    deliveries: list[DocumentFacts]
    items: list[ComparisonItem] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        counts = {status.value: 0 for status in ItemStatus}
        for item in self.items:
            counts[item.status.value] += 1
        return counts

    def by_check(self, check: str) -> list[ComparisonItem]:
        return [item for item in self.items if item.check == check]

    @property
    def currencies_differ(self) -> bool:
        currencies = {
            facts.currency
            for facts in (self.subject, self.purchase_order)
            if facts is not None and facts.currency
        }
        return len(currencies) > 1


# ------------------------------------------------------------------------------ helpers
def _side(facts: DocumentFacts, fact: FactValue | None, value: str | None = None) -> Side:
    role = ROLE_OF[facts.document_type]
    if fact is None:
        return Side(role, _id(facts), facts.display_name, value, None)
    return Side(
        role=role,
        document_id=_id(facts),
        document_label=facts.display_name,
        value=value if value is not None else fact.text(),
        printed=fact.display,
        field_id=fact.field_id,
        page=fact.page,
        source_text=fact.source_text,
        bbox=fact.bbox,
        confidence=fact.confidence,
        corrected=fact.corrected,
    )


def _id(facts: DocumentFacts) -> str | None:
    return str(facts.document_id) if facts.document_id else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _signed(value: Decimal, *, exact: bool = False) -> str:
    text = format(value if exact else value.normalize(), "f")
    return ("+" if value > 0 else "") + text


def _money(value: Decimal | None) -> str | None:
    """Amount for explanations: at least two decimals, never rounded ('38.4' -> '38.40')."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    whole, _, fraction = text.partition(".")
    return f"{whole}.{fraction.ljust(2, '0')}"


def _role_name(facts: DocumentFacts) -> str:
    return facts.document_type.value.replace("_", " ").lower()


def same_vendor(a: DocumentFacts, b: DocumentFacts, similarity: float) -> bool | None:
    """True / False, or None when either side has no vendor."""
    if a.vendor_id and b.vendor_id:
        return a.vendor_id == b.vendor_id
    key_a, key_b = a.vendor_key, b.vendor_key
    if not key_a or not key_b:
        return None
    return name_similarity(key_a, key_b) >= similarity


def match_lines(
    left: Sequence[LineFacts], right: Sequence[LineFacts], description_similarity: float
) -> tuple[list[tuple[LineFacts, LineFacts]], list[LineFacts], list[LineFacts]]:
    """Pair lines by SKU, then by description. Returns (pairs, left only, right only)."""
    pairs: list[tuple[LineFacts, LineFacts]] = []
    free = list(right)
    unmatched: list[LineFacts] = []
    for line in left:
        key = line.sku_key
        twin = next((other for other in free if key and other.sku_key == key), None)
        if twin is not None:
            pairs.append((line, twin))
            free.remove(twin)
        else:
            unmatched.append(line)
    rest: list[LineFacts] = []
    for line in unmatched:
        description = line.value("description")
        scored = [
            (fuzz.token_set_ratio(str(description), str(other.value("description"))), other)
            for other in free
            if description and other.value("description")
        ]
        best = max(scored, key=lambda pair: pair[0], default=None)
        if best is not None and best[0] >= description_similarity:
            pairs.append((line, best[1]))
            free.remove(best[1])
        else:
            rest.append(line)
    return pairs, rest, free


@dataclass(slots=True)
class _Builder:
    tolerances: Tolerances
    items: list[ComparisonItem] = field(default_factory=list)

    def numeric(
        self,
        *,
        key: str,
        category: Category,
        check: str,
        what: str,
        left: list[tuple[DocumentFacts, FactValue | None]],
        right: list[tuple[DocumentFacts, FactValue | None]],
        tolerance_abs: Decimal,
        tolerance_pct: Decimal = Decimal(0),
        line: str | None = None,
        currencies_differ: bool = False,
        unit: str = "",
        money: bool = False,
    ) -> ComparisonItem:
        """Compare the sum of the left values with the sum of the right values."""
        left_values = [fact.value for _, fact in left if fact is not None]
        right_values = [fact.value for _, fact in right if fact is not None]
        left_sum = sum(left_values, Decimal(0)) if left_values else None
        right_sum = sum(right_values, Decimal(0)) if right_values else None
        left_sides = [_side(facts, fact) for facts, fact in left]
        right_sides = [_side(facts, fact) for facts, fact in right]
        left_name = _role_name(left[0][0])
        right_name = _role_name(right[0][0])
        suffix = f" {unit}" if unit else ""
        tolerance = {
            "absolute": _text(tolerance_abs) or "0",
            "relative": _text(tolerance_pct) or "0",
        }
        if left_sum is None or right_sum is None:
            absent = left_name if left_sum is None else right_name
            item = ComparisonItem(
                key, category, check, ItemStatus.MISSING, left_sides, right_sides,
                _text(left_sum), _text(right_sum),
                f"{what}: no value on the {absent}.", line, None, tolerance,
            )  # fmt: skip
            self.items.append(item)
            return item
        difference = left_sum - right_sum
        relative = (difference / right_sum) if right_sum else None
        allowed = max(tolerance_abs, abs(right_sum) * tolerance_pct)
        diff = {"absolute": _signed(difference)}
        if relative is not None:
            diff["relative"] = _signed(relative.quantize(Decimal("0.0001")))
        weak = any(
            fact is not None and fact.uncertain(self.tolerances.min_confidence)
            for _, fact in [*left, *right]
        )
        show = _money if money else _text
        values = f"{left_name} {show(left_sum)}{suffix}, {right_name} {show(right_sum)}{suffix}"
        if currencies_differ:
            status = ItemStatus.UNCERTAIN
            explanation = f"{what}: {values}; the currencies differ and amounts are not converted."
        elif abs(difference) <= allowed:
            status = ItemStatus.MATCH
            explanation = (
                f"{what} matches: {show(left_sum)}{suffix}."
                if difference == 0
                else f"{what}: {values} (difference {_signed(difference)}, within tolerance)."
            )
        elif weak:
            status = ItemStatus.UNCERTAIN
            explanation = (
                f"{what}: {values} (difference {_signed(difference)}); a value was read with "
                "low confidence, so this may be a misread."
            )
        else:
            status = ItemStatus.MISMATCH
            percent = (
                f", {_signed((relative * 100).quantize(Decimal('0.1')), exact=True)}%"
                if relative
                else ""
            )
            explanation = f"{what}: {values} (difference {_signed(difference)}{percent})."
        item = ComparisonItem(
            key, category, check, status, left_sides, right_sides, _text(left_sum),
            _text(right_sum), explanation, line, diff, tolerance,
        )  # fmt: skip
        self.items.append(item)
        return item

    def equal(
        self,
        *,
        key: str,
        check: str,
        what: str,
        left: tuple[DocumentFacts, FactValue | None],
        right: tuple[DocumentFacts, FactValue | None],
        same: bool | None = None,
    ) -> ComparisonItem:
        """Header value equality (`same` overrides plain equality, e.g. for vendors)."""
        (left_facts, left_fact), (right_facts, right_fact) = left, right
        sides = [_side(left_facts, left_fact)], [_side(right_facts, right_fact)]
        left_text = left_fact.text() if left_fact else None
        right_text = right_fact.text() if right_fact else None
        if left_fact is None or right_fact is None:
            absent = _role_name(left_facts if left_fact is None else right_facts)
            status, explanation = ItemStatus.MISSING, f"{what}: not found on the {absent}."
        else:
            equal = same if same is not None else left_fact.value == right_fact.value
            pair = (
                f"{_role_name(left_facts)} {left_fact.display or left_text}, "
                f"{_role_name(right_facts)} {right_fact.display or right_text}"
            )
            if equal:
                status, explanation = ItemStatus.MATCH, f"{what} matches ({pair})."
            elif left_fact.uncertain(self.tolerances.min_confidence) or right_fact.uncertain(
                self.tolerances.min_confidence
            ):
                status = ItemStatus.UNCERTAIN
                explanation = f"{what} differs ({pair}), but a value was read with low confidence."
            else:
                status, explanation = ItemStatus.MISMATCH, f"{what} differs: {pair}."
        item = ComparisonItem(
            key, Category.HEADER, check, status, *sides, left_text, right_text, explanation
        )
        self.items.append(item)
        return item

    def missing_line(
        self,
        *,
        check: str,
        line: LineFacts,
        facts: DocumentFacts,
        side: str,
        what: str,
    ) -> None:
        cell = line.get("sku") or line.get("description")
        present = [_side(facts, cell, line.label)]
        left, right = (present, []) if side == "left" else ([], present)
        self.items.append(
            ComparisonItem(
                key=f"line:{line.label}:{check}",
                category=Category.LINE_ITEM,
                check=check,
                status=ItemStatus.MISSING,
                left=left,
                right=right,
                left_value=line.label if side == "left" else None,
                right_value=line.label if side == "right" else None,
                explanation=what,
                line=line.label,
            )
        )


# ------------------------------------------------------------------------------ comparisons
def _header(
    builder: _Builder, subject: DocumentFacts, reference: DocumentFacts, *, full: bool
) -> None:
    builder.equal(
        key=VENDOR,
        check=VENDOR,
        what="Vendor",
        left=(subject, subject.vendor),
        right=(reference, reference.vendor),
        same=same_vendor(subject, reference, builder.tolerances.vendor_similarity),
    )
    builder.equal(
        key=PO_REFERENCE,
        check=PO_REFERENCE,
        what="Purchase order number",
        left=(subject, subject.po_reference),
        right=(reference, reference.po_reference),
        same=subject.po_key == reference.po_key if subject.po_key and reference.po_key else None,
    )
    if not full:
        return
    builder.equal(
        key=CURRENCY,
        check=CURRENCY,
        what="Currency",
        left=(subject, subject.get("currency")),
        right=(reference, reference.get("currency")),
    )
    if subject.get("tax_rate") is not None or reference.get("tax_rate") is not None:
        builder.numeric(
            key=TAX_RATE,
            category=Category.HEADER,
            check=TAX_RATE,
            what="Tax rate",
            left=[(subject, subject.get("tax_rate"))],
            right=[(reference, reference.get("tax_rate"))],
            tolerance_abs=builder.tolerances.tax_rate_abs,
        )
    if subject.get("payment_terms_days") and reference.get("payment_terms_days"):
        builder.equal(
            key=PAYMENT_TERMS,
            check=PAYMENT_TERMS,
            what="Payment terms (days)",
            left=(subject, subject.get("payment_terms_days")),
            right=(reference, reference.get("payment_terms_days")),
        )


def _against_order(
    builder: _Builder,
    subject: DocumentFacts,
    order: DocumentFacts,
    *,
    prices: bool,
    currencies_differ: bool,
) -> None:
    tolerances = builder.tolerances
    pairs, extra, open_lines = match_lines(
        subject.lines, order.lines, tolerances.description_similarity
    )
    subject_name, order_name = _role_name(subject), _role_name(order)
    for line, ordered in pairs:
        label = line.label
        unit = str(line.value("unit") or ordered.value("unit") or "")
        if prices:
            builder.numeric(
                key=f"line:{label}:{UNIT_PRICE}",
                category=Category.LINE_ITEM,
                check=UNIT_PRICE,
                what=f"Unit price of {label}",
                left=[(subject, line.get("unit_price"))],
                right=[(order, ordered.get("unit_price"))],
                tolerance_abs=tolerances.price_abs,
                tolerance_pct=tolerances.price_pct,
                line=label,
                currencies_differ=currencies_differ,
                unit=subject.currency or "",
                money=True,
            )
        builder.numeric(
            key=f"line:{label}:{QUANTITY_ORDERED}",
            category=Category.LINE_ITEM,
            check=QUANTITY_ORDERED,
            what=f"Quantity of {label} ({subject_name} vs ordered)",
            left=[(subject, line.get("quantity"))],
            right=[(order, ordered.get("quantity"))],
            tolerance_abs=tolerances.quantity_abs,
            line=label,
            unit=unit,
        )
    for line in extra:
        builder.missing_line(
            check=LINE_ON_ORDER,
            line=line,
            facts=subject,
            side="left",
            what=f"{line.label} is on the {subject_name} but not on the {order_name}.",
        )
    for line in open_lines:
        builder.missing_line(
            check=LINE_FULFILLED,
            line=line,
            facts=order,
            side="right",
            what=f"{line.label} was ordered but is not on the {subject_name}.",
        )


def _against_deliveries(
    builder: _Builder, invoice: DocumentFacts, deliveries: Sequence[DocumentFacts]
) -> None:
    tolerances = builder.tolerances
    delivered: dict[int, list[tuple[DocumentFacts, LineFacts]]] = {}
    for note in deliveries:
        pairs, _, _ = match_lines(invoice.lines, note.lines, tolerances.description_similarity)
        for line, twin in pairs:
            delivered.setdefault(line.index, []).append((note, twin))
    for line in invoice.lines:
        label = line.label
        matches = delivered.get(line.index)
        if not matches:
            builder.missing_line(
                check=LINE_DELIVERED,
                line=line,
                facts=invoice,
                side="left",
                what=f"{label} is invoiced but on no delivery note.",
            )
            continue
        builder.numeric(
            key=f"line:{label}:{QUANTITY_DELIVERED}",
            category=Category.LINE_ITEM,
            check=QUANTITY_DELIVERED,
            what=f"Quantity of {label} (invoiced vs delivered)",
            left=[(invoice, line.get("quantity"))],
            right=[(note, twin.get("quantity")) for note, twin in matches],
            tolerance_abs=tolerances.quantity_abs,
            line=label,
            unit=str(line.value("unit") or ""),
        )


def compare_invoice(
    invoice: DocumentFacts,
    purchase_order: DocumentFacts | None,
    deliveries: Sequence[DocumentFacts],
    tolerances: Tolerances,
) -> ComparisonOutcome:
    """Two- or three-way match for an invoice; the type follows the documents available."""
    if purchase_order is not None and deliveries:
        kind = ComparisonType.INVOICE_PO_DELIVERY
    elif purchase_order is not None:
        kind = ComparisonType.INVOICE_PO
    else:
        kind = ComparisonType.INVOICE_DELIVERY
    outcome = ComparisonOutcome(kind, invoice, purchase_order, list(deliveries))
    builder = _Builder(tolerances)
    if purchase_order is not None:
        _header(builder, invoice, purchase_order, full=True)
        _against_order(
            builder,
            invoice,
            purchase_order,
            prices=True,
            currencies_differ=outcome.currencies_differ,
        )
    elif deliveries:
        _header(builder, invoice, deliveries[0], full=False)
    if deliveries:
        _against_deliveries(builder, invoice, deliveries)
    outcome.items = builder.items
    return outcome


def compare_delivery(
    delivery: DocumentFacts, purchase_order: DocumentFacts, tolerances: Tolerances
) -> ComparisonOutcome:
    """What arrived against what was ordered (short or over delivery, unknown items)."""
    outcome = ComparisonOutcome(ComparisonType.PO_DELIVERY, delivery, purchase_order, [])
    builder = _Builder(tolerances)
    _header(builder, delivery, purchase_order, full=False)
    _against_order(builder, delivery, purchase_order, prices=False, currencies_differ=False)
    outcome.items = builder.items
    return outcome

"""The default rule set, and how the configured rules set the comparison tolerances.

Migration 0005 inserts these rows (a frozen copy); administrators change parameters, severity
and on/off through the API afterwards. Tolerances live in the rule parameters only, so the
comparison view and the rule results never disagree about what "equal" means.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from docintel.db.models import DocumentType
from docintel.matching.compare import Tolerances
from docintel.rules.engine import RuleDefinition, Severity

INV = DocumentType.INVOICE
PO = DocumentType.PURCHASE_ORDER
DN = DocumentType.DELIVERY_NOTE

MANDATORY_FIELDS: dict[str, list[str]] = {
    "INVOICE": ["vendor_name", "invoice_number", "invoice_date", "total", "currency"],
    "PURCHASE_ORDER": ["po_number", "po_date", "vendor_name", "total"],
    "DELIVERY_NOTE": ["delivery_note_number", "delivery_date", "vendor_name"],
    "RECEIPT": ["merchant_name", "transaction_date", "total"],
    "CONTRACT": ["party_a", "party_b", "effective_date"],
}


def _rule(
    code: str,
    rule_type: str,
    name: str,
    description: str,
    applies_to: Iterable[DocumentType],
    severity: Severity,
    params: dict[str, Any] | None = None,
) -> RuleDefinition:
    return RuleDefinition(
        code, rule_type, name, description, frozenset(applies_to), severity, params or {}
    )


DEFAULT_RULES: tuple[RuleDefinition, ...] = (
    _rule(
        "INV_DUPLICATE", "duplicate_document", "Duplicate invoice",
        "An earlier invoice has the same vendor and number (fail), or the same vendor, amount "
        "and a date within the window (warn).",
        [INV], Severity.HIGH, {"date_window_days": 7, "match_amount_and_date": True},
    ),
    _rule(
        "INV_PO_UNIT_PRICE", "line_unit_price", "Unit price differs from the purchase order",
        "Invoice unit prices must equal the purchase order's within the tolerance.",
        [INV], Severity.HIGH, {"tolerance_pct": "0", "tolerance_abs": "0.01"},
    ),
    _rule(
        "INV_PO_QUANTITY", "line_quantity_ordered", "Invoiced quantity exceeds the order",
        "Invoiced quantities may not exceed the ordered quantities (partial invoices allowed).",
        [INV], Severity.HIGH, {"tolerance": "0", "allow_partial": True},
    ),
    _rule(
        "INV_DELIVERED_QUANTITY", "line_quantity_delivered",
        "Billed quantity exceeds the delivered quantity",
        "Three-way match: invoiced quantities may not exceed what the delivery notes record.",
        [INV], Severity.HIGH, {"tolerance": "0"},
    ),
    _rule(
        "INV_LINE_NOT_ORDERED", "line_not_ordered", "Invoice line not on the purchase order",
        "Every invoiced item must appear on the purchase order.",
        [INV], Severity.HIGH,
    ),
    _rule(
        "INV_PO_VENDOR", "header_match", "Vendor differs from the purchase order",
        "The invoice must come from the vendor the order was placed with.",
        [INV], Severity.CRITICAL, {"check": "vendor"},
    ),
    _rule(
        "INV_PO_CURRENCY", "header_match", "Currency differs from the purchase order",
        "Invoice and order must use the same currency (amounts are never converted).",
        [INV], Severity.HIGH, {"check": "currency"},
    ),
    _rule(
        "INV_PO_TAX_RATE", "header_match", "Tax rate differs from the purchase order",
        "The invoice's tax rate must equal the order's.",
        [INV], Severity.MEDIUM, {"check": "tax_rate", "tolerance": "0.0001"},
    ),
    _rule(
        "INV_PO_PAYMENT_TERMS", "header_match", "Payment terms differ from the purchase order",
        "The invoice's payment terms should equal the order's.",
        [INV], Severity.LOW, {"check": "payment_terms"},
    ),
    _rule(
        "INV_MISSING_PO", "order_reference", "Missing purchase order",
        "An invoice must reference a purchase order (fail) that is on file (warn if not yet).",
        [INV], Severity.MEDIUM, {"require_reference": True, "require_order_on_file": True},
    ),
    _rule(
        "DOC_ARITHMETIC", "document_arithmetic", "Amounts do not add up",
        "Line amounts, subtotal, tax and total must be consistent on the document itself.",
        [INV, PO, DocumentType.RECEIPT], Severity.HIGH,
    ),
    _rule(
        "DOC_MANDATORY_FIELDS", "mandatory_fields", "Mandatory fields",
        "Fields the business requires per document type.",
        [INV, PO, DN, DocumentType.RECEIPT, DocumentType.CONTRACT], Severity.MEDIUM,
        {"fields": MANDATORY_FIELDS},
    ),
    _rule(
        "DOC_UNKNOWN_VENDOR", "known_vendor", "Vendor not in the vendor master",
        "The vendor must be a known vendor (add it, or an alias, to the vendor master).",
        [INV, PO, DN], Severity.MEDIUM,
    ),
    _rule(
        "DN_PO_QUANTITY", "line_quantity_ordered", "Delivered quantity exceeds the order",
        "Delivery notes may not record more than was ordered (partial deliveries allowed).",
        [DN], Severity.MEDIUM, {"tolerance": "0", "allow_partial": True},
    ),
    _rule(
        "DN_LINE_NOT_ORDERED", "line_not_ordered", "Delivered item not on the purchase order",
        "Every delivered item must appear on the purchase order.",
        [DN], Severity.MEDIUM,
    ),
    _rule(
        "DN_PO_VENDOR", "header_match", "Delivery vendor differs from the purchase order",
        "The delivery must come from the vendor the order was placed with.",
        [DN], Severity.HIGH, {"check": "vendor"},
    ),
    _rule(
        "DN_MISSING_PO", "order_reference", "Delivery without purchase order",
        "A delivery note should reference a purchase order on file.",
        [DN], Severity.LOW, {"require_reference": True, "require_order_on_file": True},
    ),
    _rule(
        "CONTRACT_EXPIRY", "contract_expiry", "Contract expired or expiring",
        "Fails after the expiration date; warns within the notice window or for auto-renewals.",
        [DocumentType.CONTRACT], Severity.HIGH, {"warn_within_days": 30},
    ),
    _rule(
        "POLICY_PAYMENT_TERMS", "payment_terms_limit", "Payment terms above policy",
        "Company policy caps vendor payment terms.",
        [INV], Severity.MEDIUM, {"max_days": 60},
    ),
)  # fmt: skip


def tolerances_from_rules(rules: Iterable[RuleDefinition], *, min_confidence: float) -> Tolerances:
    """Comparison tolerances from the enabled invoice rules (defaults where a rule is off)."""
    base = Tolerances(min_confidence=min_confidence)
    price_abs, price_pct = base.price_abs, base.price_pct
    quantity, tax = base.quantity_abs, base.tax_rate_abs
    for rule in rules:
        if not rule.enabled or INV not in rule.applies_to:
            continue
        params = rule.params
        if rule.rule_type == "line_unit_price":
            price_abs = Decimal(str(params.get("tolerance_abs", price_abs)))
            price_pct = Decimal(str(params.get("tolerance_pct", price_pct)))
        elif rule.rule_type == "line_quantity_ordered":
            quantity = Decimal(str(params.get("tolerance", quantity)))
        elif rule.rule_type == "header_match" and params.get("check") == "tax_rate":
            if params.get("tolerance") is not None:
                tax = Decimal(str(params["tolerance"]))
    return Tolerances(
        price_abs=price_abs,
        price_pct=price_pct,
        quantity_abs=quantity,
        tax_rate_abs=tax,
        min_confidence=min_confidence,
    )


def duplicate_params(rules: Iterable[RuleDefinition]) -> dict[str, Any] | None:
    """Parameters of the enabled duplicate rule, None if duplicate detection is off."""
    for rule in rules:
        if rule.enabled and rule.rule_type == "duplicate_document":
            return dict(rule.params)
    return None

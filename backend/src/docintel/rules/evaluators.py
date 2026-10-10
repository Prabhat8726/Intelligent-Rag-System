"""Rule types. Each evaluator reads facts, comparison items, duplicate matches or (contracts)
the deterministic clause segmentation - never free page text - so a rule can only report what
the deterministic layers established."""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any, ClassVar

from pydantic import Field, StringConstraints, model_validator

from docintel.db.models import DocumentType
from docintel.fields.schemas import SCHEMA_INFO
from docintel.matching import compare as checks
from docintel.matching.compare import ComparisonItem, ItemStatus
from docintel.matching.facts import ClauseFact, find_clause
from docintel.rules.engine import Finding, Outcome, RuleContext, RuleParams, register

_SHOWN = 3  # findings named in a message; the evidence lists all of them


def _items_message(items: list[ComparisonItem], lead: str) -> str:
    named = "; ".join(item.explanation.rstrip(".") for item in items[:_SHOWN])
    more = f" (and {len(items) - _SHOWN} more)" if len(items) > _SHOWN else ""
    return f"{lead}: {named}{more}."


def _comparison_finding(
    items: list[ComparisonItem],
    *,
    failing: list[ComparisonItem],
    fail_lead: str,
    ok_message: str,
) -> Finding:
    uncertain = [item for item in items if item.status == ItemStatus.UNCERTAIN]
    if failing:
        return Finding(
            Outcome.FAIL,
            _items_message(failing, fail_lead),
            {"items": [item.to_json() for item in failing]},
            [item.key for item in failing],
        )
    if uncertain:
        return Finding(
            Outcome.WARN,
            _items_message(uncertain, "Could not be verified"),
            {"items": [item.to_json() for item in uncertain]},
            [item.key for item in uncertain],
        )
    return Finding(Outcome.PASS, ok_message)


def _difference(item: ComparisonItem) -> Decimal:
    return Decimal((item.difference or {}).get("absolute", "0"))


# ------------------------------------------------------------------------------ duplicates
class DuplicateParams(RuleParams):
    date_window_days: int = Field(default=7, ge=0, le=365)
    match_amount_and_date: bool = True


class DuplicateDocument:
    rule_type: ClassVar[str] = "duplicate_document"
    params_model: ClassVar[type[RuleParams]] = DuplicateParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        if not context.duplicates:
            return Finding(Outcome.PASS, "No earlier document with the same identity was found.")
        strong = [match for match in context.duplicates if match.strong]
        shown = strong or context.duplicates
        names = ", ".join(match.label for match in shown[:_SHOWN])
        evidence = {"duplicates": [match.to_json() for match in context.duplicates]}
        if strong:
            return Finding(
                Outcome.FAIL,
                f"Duplicate of {names} (same vendor and document number).",
                evidence,
            )
        return Finding(
            Outcome.WARN,
            f"Possible duplicate of {names} (same vendor, amount and date).",
            evidence,
        )


# ------------------------------------------------------------------------------ comparisons
class PriceParams(RuleParams):
    tolerance_pct: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    tolerance_abs: Decimal = Field(default=Decimal("0.01"), ge=0)


class LineUnitPrice:
    rule_type: ClassVar[str] = "line_unit_price"
    params_model: ClassVar[type[RuleParams]] = PriceParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        comparison = context.comparison
        if comparison is None or comparison.purchase_order is None:
            return Finding(Outcome.NOT_APPLICABLE, "No purchase order to compare prices with.")
        items = comparison.by_check(checks.UNIT_PRICE)
        if not items:
            return Finding(Outcome.NOT_APPLICABLE, "No line matched the purchase order.")
        return _comparison_finding(
            items,
            failing=[item for item in items if item.status == ItemStatus.MISMATCH],
            fail_lead="Unit price differs from the purchase order",
            ok_message="Every unit price matches the purchase order.",
        )


class QuantityParams(RuleParams):
    tolerance: Decimal = Field(default=Decimal("0"), ge=0)
    allow_partial: bool = True


class LineQuantityOrdered:
    """Invoiced (or delivered) quantity against the ordered quantity."""

    rule_type: ClassVar[str] = "line_quantity_ordered"
    params_model: ClassVar[type[RuleParams]] = QuantityParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        comparison = context.comparison
        if comparison is None or comparison.purchase_order is None:
            return Finding(Outcome.NOT_APPLICABLE, "No purchase order to compare quantities with.")
        items = comparison.by_check(checks.QUANTITY_ORDERED)
        if not items:
            return Finding(Outcome.NOT_APPLICABLE, "No line matched the purchase order.")
        failing = [
            item
            for item in items
            if item.status == ItemStatus.MISMATCH
            and (
                _difference(item) > params.tolerance
                or (not params.allow_partial and abs(_difference(item)) > params.tolerance)
            )
        ]
        what = "Invoiced" if context.document.document_type == DocumentType.INVOICE else "Delivered"
        return _comparison_finding(
            items,
            failing=failing,
            fail_lead=f"{what} quantity exceeds the ordered quantity"
            if params.allow_partial
            else f"{what} quantity differs from the ordered quantity",
            ok_message=f"{what} quantities are within the ordered quantities.",
        )


class DeliveredParams(RuleParams):
    tolerance: Decimal = Field(default=Decimal("0"), ge=0)


class LineQuantityDelivered:
    rule_type: ClassVar[str] = "line_quantity_delivered"
    params_model: ClassVar[type[RuleParams]] = DeliveredParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        comparison = context.comparison
        if comparison is None or not comparison.deliveries:
            return Finding(Outcome.NOT_APPLICABLE, "No delivery note for this order yet.")
        # A billed line on no delivery note counts as delivered quantity zero.
        items = [
            *comparison.by_check(checks.QUANTITY_DELIVERED),
            *comparison.by_check(checks.LINE_DELIVERED),
        ]
        failing = [
            item
            for item in items
            if (item.status == ItemStatus.MISMATCH and _difference(item) > params.tolerance)
            or (item.check == checks.LINE_DELIVERED and item.status == ItemStatus.MISSING)
        ]
        return _comparison_finding(
            items,
            failing=failing,
            fail_lead="Billed quantity exceeds the delivered quantity",
            ok_message="No line is billed above the delivered quantity.",
        )


class NoParams(RuleParams):
    pass


class LineNotOrdered:
    rule_type: ClassVar[str] = "line_not_ordered"
    params_model: ClassVar[type[RuleParams]] = NoParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        comparison = context.comparison
        if comparison is None or comparison.purchase_order is None:
            return Finding(Outcome.NOT_APPLICABLE, "No purchase order to compare lines with.")
        items = comparison.by_check(checks.LINE_ON_ORDER)
        return _comparison_finding(
            items,
            failing=[item for item in items if item.status == ItemStatus.MISSING],
            fail_lead="Lines not on the purchase order",
            ok_message="Every line is on the purchase order.",
        )


_HEADER_CHECKS = (checks.VENDOR, checks.CURRENCY, checks.TAX_RATE, checks.PAYMENT_TERMS)


class HeaderParams(RuleParams):
    check: str = Field(pattern="^(" + "|".join(_HEADER_CHECKS) + ")$")
    tolerance: Decimal | None = Field(default=None, ge=0)  # tax rate only


class HeaderMatch:
    """A header value must agree with the purchase order (vendor, currency, tax rate, terms)."""

    rule_type: ClassVar[str] = "header_match"
    params_model: ClassVar[type[RuleParams]] = HeaderParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        comparison = context.comparison
        if comparison is None or comparison.purchase_order is None:
            return Finding(Outcome.NOT_APPLICABLE, "No purchase order to compare with.")
        items = comparison.by_check(params.check)
        if not items or all(item.status == ItemStatus.MISSING for item in items):
            return Finding(Outcome.NOT_APPLICABLE, "The value is not on both documents.")
        return _comparison_finding(
            items,
            failing=[item for item in items if item.status == ItemStatus.MISMATCH],
            fail_lead="Differs from the purchase order",
            ok_message="Matches the purchase order.",
        )


class OrderReferenceParams(RuleParams):
    require_reference: bool = True
    require_order_on_file: bool = True


class OrderReference:
    rule_type: ClassVar[str] = "order_reference"
    params_model: ClassVar[type[RuleParams]] = OrderReferenceParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        reference = context.document.po_reference
        if reference is None:
            if params.require_reference:
                return Finding(Outcome.FAIL, "No purchase order number on the document.")
            return Finding(Outcome.PASS, "A purchase order reference is not required.")
        if context.order_on_file is False and params.require_order_on_file:
            return Finding(
                Outcome.WARN,
                f"Purchase order {reference.display or reference.value} is not on file "
                "(not uploaded yet, or in another department).",
                {"reference": reference.display},
            )
        return Finding(Outcome.PASS, f"References purchase order {reference.display}.")


# ------------------------------------------------------------------------------ single document
class ArithmeticParams(RuleParams):
    checks: list[str] = Field(
        default_factory=lambda: ["LINE_AMOUNT", "LINES_SUM", "TOTAL_ARITHMETIC", "TAX_RATE"],
        min_length=1,
    )


class DocumentArithmetic:
    rule_type: ClassVar[str] = "document_arithmetic"
    params_model: ClassVar[type[RuleParams]] = ArithmeticParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        document = context.document
        failed = [check for check in document.failed_checks if check.get("code") in params.checks]

        def unverified(check: dict[str, Any]) -> bool:
            # A sum that is off because a value was misread (or not read) is not a document
            # error: like a comparison, a weak reading can only ask for a check.
            values = [document.at(str(path)) for path in check.get("fields", [])]
            return any(v is None or v.uncertain(context.min_confidence) for v in values)

        confirmed = [check for check in failed if not unverified(check)]
        shown = confirmed or failed
        messages = "; ".join(str(check.get("message")) for check in shown[:_SHOWN])
        if confirmed:
            return Finding(Outcome.FAIL, f"Amounts do not add up: {messages}.", {"checks": failed})
        if failed:
            return Finding(
                Outcome.WARN,
                f"Could not be verified (a value involved was read with low confidence): "
                f"{messages}.",
                {"checks": failed},
            )
        return Finding(Outcome.PASS, "Amounts add up.")


class MandatoryFieldsParams(RuleParams):
    fields: dict[DocumentType, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_fields(self) -> MandatoryFieldsParams:
        for document_type, names in self.fields.items():
            schema = SCHEMA_INFO.get(document_type)
            known = {scalar.name for scalar in schema.scalars} if schema else set()
            unknown = [name for name in names if name not in known]
            if unknown:
                msg = f"{document_type.value} has no field(s) {', '.join(unknown)}"
                raise ValueError(msg)
        return self


class MandatoryFields:
    rule_type: ClassVar[str] = "mandatory_fields"
    params_model: ClassVar[type[RuleParams]] = MandatoryFieldsParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        names = params.fields.get(context.document.document_type, [])
        if not names:
            return Finding(Outcome.NOT_APPLICABLE, "No mandatory fields configured for this type.")
        missing = [name for name in names if context.document.get(name) is None]
        if missing:
            return Finding(
                Outcome.FAIL,
                f"Mandatory fields missing: {', '.join(missing)}.",
                {"missing": missing},
            )
        return Finding(Outcome.PASS, "All mandatory fields are present.")


class KnownVendor:
    rule_type: ClassVar[str] = "known_vendor"
    params_model: ClassVar[type[RuleParams]] = NoParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        vendor = context.document.vendor
        if vendor is None:
            return Finding(Outcome.NOT_APPLICABLE, "No vendor name on the document.")
        if context.document.vendor_id is None:
            return Finding(
                Outcome.WARN,
                f"Vendor '{vendor.display}' is not in the vendor master.",
                {"vendor": vendor.display},
            )
        return Finding(Outcome.PASS, f"Vendor is {context.document.vendor_name}.")


class ContractExpiryParams(RuleParams):
    warn_within_days: int = Field(default=30, ge=0, le=3650)


class ContractExpiry:
    rule_type: ClassVar[str] = "contract_expiry"
    params_model: ClassVar[type[RuleParams]] = ContractExpiryParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        expires = context.document.value("expiration_date")
        if expires is None:
            return Finding(Outcome.NOT_APPLICABLE, "No expiration date on the contract.")
        today = context.reference_date
        renews = context.document.value("auto_renewal") is True
        if expires < today:
            outcome = Outcome.WARN if renews else Outcome.FAIL
            suffix = " (it renews automatically - check the renewal)" if renews else ""
            return Finding(
                outcome,
                f"The contract expired on {expires.isoformat()}{suffix}.",
                {"expiration_date": expires.isoformat(), "reference_date": today.isoformat()},
            )
        if expires <= today + timedelta(days=params.warn_within_days):
            return Finding(
                Outcome.WARN,
                f"The contract expires on {expires.isoformat()}, within "
                f"{params.warn_within_days} days.",
                {"expiration_date": expires.isoformat(), "reference_date": today.isoformat()},
            )
        return Finding(Outcome.PASS, f"The contract runs until {expires.isoformat()}.")


class PaymentTermsParams(RuleParams):
    max_days: int = Field(default=60, ge=0, le=3650)


class PaymentTermsLimit:
    """Policy: payment terms may not exceed a maximum (from the terms, or due - issue date)."""

    rule_type: ClassVar[str] = "payment_terms_limit"
    params_model: ClassVar[type[RuleParams]] = PaymentTermsParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        document = context.document
        days = document.value("payment_terms_days")
        issued, due = document.document_date, document.value("due_date")
        if days is None and issued is not None and due is not None:
            days = (due - issued).days
        if days is None:
            return Finding(Outcome.NOT_APPLICABLE, "No payment terms on the document.")
        if days > params.max_days:
            return Finding(
                Outcome.FAIL,
                f"Payment terms of {days} days exceed the policy maximum of {params.max_days}.",
                {"days": days, "max_days": params.max_days},
            )
        return Finding(Outcome.PASS, f"Payment terms of {days} days are within policy.")


# ------------------------------------------------------------------------------ contract clauses
ClauseKeyword = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=60)
]
# "60 days written notice", "90 days' prior written notice", "thirty (30) days notice"
_NOTICE_DAYS = re.compile(
    r"(\d{1,4})\s*\)?\s*(?:calendar\s+|business\s+)?days?['\u2019]?\s+"
    r"(?:prior\s+)?(?:written\s+)?(?:advance\s+)?notice",
    re.IGNORECASE,
)
_NOTICE_OF = re.compile(r"notice\s+(?:period\s+)?of\s+(\d{1,4})\s*(?:calendar\s+)?days", re.I)
_LAW_OF = re.compile(r"laws?\s+of\s+(?P<place>[^.;\n]{2,80})", re.IGNORECASE)


def _clause_evidence(clause: ClauseFact) -> dict[str, Any]:
    return {"clause": clause.label, "page": clause.page}


def _no_clauses(clauses: list[ClauseFact] | None) -> Finding | None:
    if clauses is None:
        return Finding(Outcome.NOT_APPLICABLE, "The contract text was not available.")
    if not clauses:
        return Finding(
            Outcome.WARN,
            "No numbered clauses were found in the contract; check it manually.",
        )
    return None


class RequiredClausesParams(RuleParams):
    clauses: list[ClauseKeyword] = Field(min_length=1, max_length=20)


class RequiredClauses:
    """Policy: a contract must contain clauses whose titles name each keyword."""

    rule_type: ClassVar[str] = "required_clauses"
    params_model: ClassVar[type[RuleParams]] = RequiredClausesParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        clauses = context.document.clauses
        if (finding := _no_clauses(clauses)) is not None:
            return finding
        assert clauses is not None  # noqa: S101 - _no_clauses handled None
        found = {keyword: find_clause(clauses, keyword) for keyword in params.clauses}
        missing = [keyword for keyword, clause in found.items() if clause is None]
        present = {k: c.label for k, c in found.items() if c is not None}
        if missing:
            return Finding(
                Outcome.FAIL,
                f"Required clause(s) missing: {', '.join(missing)}.",
                {"missing": missing, "present": present},
            )
        return Finding(
            Outcome.PASS,
            f"All required clauses are present ({'; '.join(present.values())}).",
            {"present": present},
        )


class NoticePeriodParams(RuleParams):
    clause: ClauseKeyword = "Termination"
    max_days: int = Field(default=90, ge=1, le=3650)


def notice_days(text: str) -> tuple[int, str] | None:
    """The longest notice period stated in days, with the words it was read from."""
    found = [
        (int(match.group(1)), match.group(0))
        for pattern in (_NOTICE_DAYS, _NOTICE_OF)
        for match in pattern.finditer(text)
    ]
    return max(found, key=lambda item: item[0]) if found else None


class NoticePeriodLimit:
    """Policy: the notice period in a clause (termination for convenience) has a maximum."""

    rule_type: ClassVar[str] = "clause_notice_period"
    params_model: ClassVar[type[RuleParams]] = NoticePeriodParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        extracted = context.document.get("termination_notice_days")
        clauses = context.document.clauses
        clause = find_clause(clauses, params.clause) if clauses else None
        days: int | None = None
        evidence: dict[str, Any] = {"max_days": params.max_days}
        if extracted is not None and isinstance(extracted.value, int):
            days = extracted.value
            evidence |= {"source": "extracted field", "page": extracted.page}
        elif clause is not None:
            read = notice_days(clause.text)
            if read is not None:
                days = read[0]
                evidence |= {**_clause_evidence(clause), "quote": read[1]}
        if days is None:
            if clause is None:
                return Finding(Outcome.NOT_APPLICABLE, f"No {params.clause.lower()} clause.")
            return Finding(
                Outcome.WARN,
                f"Clause {clause.label} states no notice period in days; check it manually.",
                _clause_evidence(clause),
            )
        evidence["days"] = days
        if days > params.max_days:
            return Finding(
                Outcome.FAIL,
                f"A notice period of {days} days exceeds the maximum of {params.max_days}.",
                evidence,
            )
        return Finding(
            Outcome.PASS,
            f"A notice period of {days} days is within the maximum of {params.max_days}.",
            evidence,
        )


class GoverningLawParams(RuleParams):
    clause: ClauseKeyword = "Governing Law"
    allowed: list[ClauseKeyword] = Field(min_length=1, max_length=10)


class GoverningLaw:
    """Policy: contracts are governed by an approved law; another one needs Legal (WARN)."""

    rule_type: ClassVar[str] = "governing_law"
    params_model: ClassVar[type[RuleParams]] = GoverningLawParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        clauses = context.document.clauses
        clause = find_clause(clauses, params.clause) if clauses else None
        if clause is None:
            return Finding(Outcome.NOT_APPLICABLE, f"No {params.clause.lower()} clause.")
        text = " ".join(clause.text.split())
        match = _LAW_OF.search(text)
        place = match.group("place").strip() if match else None
        evidence = {**_clause_evidence(clause), "jurisdiction": place, "allowed": params.allowed}
        folded = text.casefold()
        if any(allowed.casefold() in folded for allowed in params.allowed):
            return Finding(
                Outcome.PASS, f"Governed by the laws of {place or 'an approved law'}.", evidence
            )
        if place is None:
            return Finding(
                Outcome.WARN,
                f"Clause {clause.label} names no governing law; check it manually.",
                evidence,
            )
        return Finding(
            Outcome.WARN,
            f"Governed by the laws of {place}, not {' or '.join(params.allowed)}: "
            "another jurisdiction needs Legal's approval.",
            evidence,
        )


for _evaluator in (
    DuplicateDocument(),
    LineUnitPrice(),
    LineQuantityOrdered(),
    LineQuantityDelivered(),
    LineNotOrdered(),
    HeaderMatch(),
    OrderReference(),
    DocumentArithmetic(),
    MandatoryFields(),
    KnownVendor(),
    ContractExpiry(),
    PaymentTermsLimit(),
    RequiredClauses(),
    NoticePeriodLimit(),
    GoverningLaw(),
):
    register(_evaluator)

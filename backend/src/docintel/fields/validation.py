"""Consistency checks on extracted values (arithmetic and date logic).

A check that passes corroborates every value it involves; a value whose every check fails is
suspect. The checks do not decide which value is wrong (a printed total can be wrong, or the
extraction can be): they lower confidence and send the document to review. Named business
discrepancies (TOTAL_MISMATCH, ...) are the rule engine's job in Phase 5.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

CENT = Decimal("0.01")
DEFAULT_TOLERANCE = Decimal("0.01")


class CheckStatus(StrEnum):
    PASS = "PASS"  # noqa: S105  (a check result, not a password)
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    code: str
    status: CheckStatus
    fields: tuple[str, ...]
    expected: str
    actual: str
    message: str

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status.value,
            "fields": list(self.fields),
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
        }


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _compare(
    code: str,
    fields: tuple[str, ...],
    expected: Decimal,
    actual: Decimal,
    tolerance: Decimal,
    description: str,
) -> Check:
    ok = abs(expected - actual) <= tolerance
    return Check(
        code,
        CheckStatus.PASS if ok else CheckStatus.FAIL,
        fields,
        str(expected),
        str(actual),
        description if ok else f"{description}: expected {expected}, printed {actual}",
    )


def run_checks(
    scalars: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    table: str | None,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> list[Check]:
    """`scalars` / `rows` hold normalized values (strings for decimals and ISO dates)."""
    checks: list[Check] = []
    amounts: list[Decimal] = []
    for index, row in enumerate(rows):
        quantity, price = _decimal(row.get("quantity")), _decimal(row.get("unit_price"))
        amount = _decimal(row.get("amount"))
        if amount is not None:
            amounts.append(amount)
        if quantity is not None and price is not None and amount is not None:
            prefix = f"{table}[{index}]"
            checks.append(
                _compare(
                    "LINE_AMOUNT",
                    (f"{prefix}.quantity", f"{prefix}.unit_price", f"{prefix}.amount"),
                    _money(quantity * price),
                    amount,
                    tolerance,
                    f"line {index + 1}: quantity x unit price = amount",
                )
            )

    subtotal, tax = _decimal(scalars.get("subtotal")), _decimal(scalars.get("tax_amount"))
    total, rate = _decimal(scalars.get("total")), _decimal(scalars.get("tax_rate"))
    if amounts and len(amounts) == len(rows) and table is not None:
        target_name = "subtotal" if subtotal is not None else None
        target = subtotal
        if target is None and tax is None and total is not None:
            target_name, target = "total", total
        if target is not None and target_name is not None:
            checks.append(
                _compare(
                    "LINES_SUM",
                    (*(f"{table}[{i}].amount" for i in range(len(rows))), target_name),
                    sum(amounts, Decimal(0)),
                    target,
                    tolerance * max(1, len(amounts) // 10 + 1),
                    f"sum of line amounts = {target_name}",
                )
            )
    if subtotal is not None and tax is not None and total is not None:
        checks.append(
            _compare(
                "TOTAL_ARITHMETIC",
                ("subtotal", "tax_amount", "total"),
                subtotal + tax,
                total,
                tolerance,
                "subtotal + tax = total",
            )
        )
    if subtotal is not None and rate is not None and tax is not None:
        checks.append(
            _compare(
                "TAX_RATE",
                ("subtotal", "tax_rate", "tax_amount"),
                _money(subtotal * rate),
                tax,
                tolerance,
                "subtotal x tax rate = tax",
            )
        )

    issued = _date(scalars.get("invoice_date"))
    due = _date(scalars.get("due_date"))
    terms = scalars.get("payment_terms_days")
    if issued is not None and due is not None:
        if isinstance(terms, int):
            expected_due = issued + timedelta(days=terms)
            ok = expected_due == due
            checks.append(
                Check(
                    "DUE_DATE_TERMS",
                    CheckStatus.PASS if ok else CheckStatus.FAIL,
                    ("invoice_date", "payment_terms_days", "due_date"),
                    expected_due.isoformat(),
                    due.isoformat(),
                    "invoice date + payment terms = due date"
                    if ok
                    else f"invoice date + {terms} days is {expected_due}, due date is {due}",
                )
            )
        else:
            checks.append(_order("DUE_AFTER_ISSUE", "invoice_date", issued, "due_date", due))

    for start_name, end_name in (
        ("statement_period_start", "statement_period_end"),
        ("effective_date", "expiration_date"),
    ):
        start, end = _date(scalars.get(start_name)), _date(scalars.get(end_name))
        if start is not None and end is not None:
            checks.append(_order("DATE_ORDER", start_name, start, end_name, end))

    opening = _decimal(scalars.get("opening_balance"))
    closing = _decimal(scalars.get("closing_balance"))
    if opening is not None and closing is not None:
        credits = _decimal(scalars.get("total_credits"))
        debits = _decimal(scalars.get("total_debits"))
        fields: tuple[str, ...] = ("opening_balance", "total_credits", "total_debits")
        if credits is None or debits is None:
            credits = sum((_decimal(row.get("credit")) or Decimal(0) for row in rows), Decimal(0))
            debits = sum(
                (abs(_decimal(row.get("debit")) or Decimal(0)) for row in rows), Decimal(0)
            )
            fields = ("opening_balance",)
            if not rows:
                credits = debits = None
        if credits is not None and debits is not None:
            checks.append(
                _compare(
                    "BALANCE_RECONCILES",
                    (*fields, "closing_balance"),
                    opening + credits - abs(debits),
                    closing,
                    tolerance,
                    "opening balance + credits - debits = closing balance",
                )
            )
    return checks


def _order(code: str, first_name: str, first: date, second_name: str, second: date) -> Check:
    ok = first <= second
    return Check(
        code,
        CheckStatus.PASS if ok else CheckStatus.FAIL,
        (first_name, second_name),
        f"{first_name} <= {second_name}",
        f"{first.isoformat()} / {second.isoformat()}",
        f"{first_name} is not after {second_name}"
        if ok
        else f"{first_name} {first} is after {second_name} {second}",
    )


def consistency_by_field(checks: Sequence[Check]) -> dict[str, bool]:
    """Per field: True if at least one check involving it passed, False if all failed."""
    passed: dict[str, bool] = {}
    for check in checks:
        for name in check.fields:
            ok = check.status == CheckStatus.PASS
            passed[name] = passed.get(name, False) or ok
    return passed

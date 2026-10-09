"""Review items: what a person has to look at, with stable keys, priority and task type."""

from __future__ import annotations

from docintel.db.models import ReviewPriority, ReviewTaskType, RuleSeverity
from docintel.review.items import (
    document_reasons,
    ordered,
    priority,
    processing_items,
    rule_items,
    task_type,
)
from docintel.rules.defaults import DEFAULT_RULES
from docintel.rules.engine import Outcome, RuleResult

RULES = {rule.code: rule for rule in DEFAULT_RULES}


def result(
    code: str, outcome: Outcome, items: list[str] | None = None, **evidence: object
) -> RuleResult:
    return RuleResult(RULES[code], outcome, f"{code} {outcome.value}", dict(evidence), items or [])


def test_processing_reasons_become_items() -> None:
    items = processing_items(["EXTRACTION_UNCERTAIN", "CLASSIFICATION_UNCERTAIN", "BOGUS"])
    assert [(i.key, i.category, i.severity) for i in items] == [
        ("reason:EXTRACTION_UNCERTAIN", "EXTRACTION", RuleSeverity.MEDIUM),
        ("reason:CLASSIFICATION_UNCERTAIN", "CLASSIFICATION", RuleSeverity.MEDIUM),
    ]
    mandatory = processing_items(["EXTRACTION_UNCERTAIN"], review_level="MANDATORY_REVIEW")
    assert mandatory[0].severity == RuleSeverity.HIGH


def test_rule_items_are_keyed_by_what_failed() -> None:
    price = result("INV_PO_UNIT_PRICE", Outcome.FAIL, ["line:A:unit_price", "line:B:unit_price"])
    warn = result("INV_PO_UNIT_PRICE", Outcome.WARN, ["line:A:unit_price"])
    passed = result("INV_MISSING_PO", Outcome.PASS)
    dup = result(
        "INV_DUPLICATE", Outcome.FAIL, duplicates=[{"document_id": "d-2"}, {"document_id": "d-1"}]
    )
    items = rule_items([price, warn, passed, dup])
    assert [i.key for i in items] == [
        "rule:INV_PO_UNIT_PRICE:FAIL:line:A:unit_price,line:B:unit_price",
        "rule:INV_PO_UNIT_PRICE:WARN:line:A:unit_price",
        "rule:INV_DUPLICATE:FAIL:d-1,d-2",
    ]
    assert items[1].severity == RuleSeverity.MEDIUM  # "could not verify" is softer than HIGH
    assert [i.category for i in items] == ["DISCREPANCY", "DISCREPANCY", "DUPLICATE"]
    assert document_reasons(["EXTRACTION_UNCERTAIN", "RULE_VIOLATION"], items) == [
        "EXTRACTION_UNCERTAIN",
        "RULE_VIOLATION",
        "DUPLICATE_SUSPECTED",
    ]
    assert priority(items) == ReviewPriority.HIGH
    assert task_type(items) == ReviewTaskType.DUPLICATE_REVIEW
    vendor = rule_items([result("INV_PO_VENDOR", Outcome.FAIL, ["vendor"])])
    assert priority(vendor) == ReviewPriority.URGENT
    assert (
        ordered([*processing_items(["CLASSIFICATION_UNCERTAIN"]), *vendor])[0].code
        == "INV_PO_VENDOR"
    )


def test_long_keys_are_shortened_stably() -> None:
    many = [f"line:SKU-{n:05d}-LONG-DESCRIPTION:unit_price" for n in range(40)]
    first = rule_items([result("INV_PO_UNIT_PRICE", Outcome.FAIL, many)])[0].key
    again = rule_items([result("INV_PO_UNIT_PRICE", Outcome.FAIL, many)])[0].key
    assert len(first) <= 300
    assert first == again

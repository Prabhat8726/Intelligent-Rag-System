"""Review items: processing reasons and rule outcomes as things a person should check."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from docintel.db.models import (
    ReviewPriority,
    ReviewReason,
    ReviewTaskType,
    RuleOutcome,
    RuleSeverity,
)
from docintel.rules.engine import SEVERITY_RANK, RuleResult

# Category of a finding -> the task type it makes (most important first).
CATEGORY_TASK: dict[str, ReviewTaskType] = {
    "DUPLICATE": ReviewTaskType.DUPLICATE_REVIEW,
    "DISCREPANCY": ReviewTaskType.DISCREPANCY_REVIEW,
    "EXTRACTION": ReviewTaskType.EXTRACTION_REVIEW,
    "CONTENT": ReviewTaskType.EXTRACTION_REVIEW,
    "CLASSIFICATION": ReviewTaskType.CLASSIFICATION_REVIEW,
    "REQUESTED": ReviewTaskType.REQUESTED_REVIEW,
}
_TASK_ORDER = list(dict.fromkeys(CATEGORY_TASK.values()))

REASONS: dict[ReviewReason, tuple[str, RuleSeverity, str]] = {
    ReviewReason.NO_TEXT_FOUND: ("CONTENT", RuleSeverity.HIGH, "No readable text was found."),
    ReviewReason.OCR_FAILED: (
        "CONTENT",
        RuleSeverity.HIGH,
        "Text recognition failed on at least one page.",
    ),
    ReviewReason.LOW_OCR_CONFIDENCE: (
        "CONTENT",
        RuleSeverity.MEDIUM,
        "Text recognition confidence is low on at least one page.",
    ),
    ReviewReason.CLASSIFICATION_UNCERTAIN: (
        "CLASSIFICATION",
        RuleSeverity.MEDIUM,
        "The document type is uncertain.",
    ),
    ReviewReason.EXTRACTION_FAILED: (
        "EXTRACTION",
        RuleSeverity.HIGH,
        "No fields could be extracted.",
    ),
    ReviewReason.MISSING_REQUIRED_FIELDS: (
        "EXTRACTION",
        RuleSeverity.HIGH,
        "Required fields are missing.",
    ),
    ReviewReason.EXTRACTION_UNCERTAIN: (
        "EXTRACTION",
        RuleSeverity.MEDIUM,
        "Some extracted values are uncertain.",
    ),
    ReviewReason.EXTRACTION_INCONSISTENT: (
        "EXTRACTION",
        RuleSeverity.HIGH,
        "Extracted amounts or dates do not add up.",
    ),
}
# Reasons the rules produce (recomputed on every evaluation; never carried over).
RULE_REASONS = frozenset({ReviewReason.RULE_VIOLATION, ReviewReason.DUPLICATE_SUSPECTED})

PRIORITY_OF: dict[RuleSeverity, ReviewPriority] = {
    RuleSeverity.CRITICAL: ReviewPriority.URGENT,
    RuleSeverity.HIGH: ReviewPriority.HIGH,
    RuleSeverity.MEDIUM: ReviewPriority.NORMAL,
    RuleSeverity.LOW: ReviewPriority.LOW,
}
_SOFTER = {
    RuleSeverity.CRITICAL: RuleSeverity.HIGH,
    RuleSeverity.HIGH: RuleSeverity.MEDIUM,
    RuleSeverity.MEDIUM: RuleSeverity.LOW,
    RuleSeverity.LOW: RuleSeverity.LOW,
}
_KEY_LIMIT = 300


@dataclass(frozen=True, slots=True)
class ReviewItem:
    key: str  # stable identity of the finding (what a reviewer's decision covers)
    category: str
    code: str
    severity: RuleSeverity
    message: str

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ReviewItem:
        return cls(
            key=str(data["key"]),
            category=str(data["category"]),
            code=str(data["code"]),
            severity=RuleSeverity(data["severity"]),
            message=str(data["message"]),
        )


def _key(*parts: str) -> str:
    key = ":".join(part for part in parts if part)
    if len(key) <= _KEY_LIMIT:
        return key
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return f"{key[: _KEY_LIMIT - 17]}#{digest}"


def processing_items(
    reasons: Iterable[str], *, review_level: str | None = None
) -> list[ReviewItem]:
    items: list[ReviewItem] = []
    for code in dict.fromkeys(reasons):
        try:
            reason = ReviewReason(code)
        except ValueError:
            continue
        if reason not in REASONS:
            continue
        category, severity, message = REASONS[reason]
        if reason == ReviewReason.EXTRACTION_UNCERTAIN and review_level == "MANDATORY_REVIEW":
            severity = RuleSeverity.HIGH
        items.append(ReviewItem(_key("reason", code), category, code, severity, message))
    return items


def rule_items(results: Iterable[RuleResult]) -> list[ReviewItem]:
    items: list[ReviewItem] = []
    for result in results:
        if not result.needs_review:
            continue
        duplicate = result.rule.rule_type == "duplicate_document"
        if duplicate:
            subject = ",".join(
                sorted(str(match["document_id"]) for match in result.evidence.get("duplicates", []))
            )
        else:
            subject = ",".join(sorted(result.items))
        severity = result.severity
        if result.outcome == RuleOutcome.WARN:
            severity = _SOFTER[severity]  # "could not verify" is less urgent than a mismatch
        elif result.outcome == RuleOutcome.ERROR:
            severity = RuleSeverity.MEDIUM
        items.append(
            ReviewItem(
                key=_key("rule", result.rule.code, result.outcome.value, subject),
                category="DUPLICATE" if duplicate else "DISCREPANCY",
                code=result.rule.code,
                severity=severity,
                message=result.message,
            )
        )
    return items


REQUEST_SEVERITY: dict[ReviewPriority, RuleSeverity] = {
    ReviewPriority.HIGH: RuleSeverity.HIGH,
    ReviewPriority.NORMAL: RuleSeverity.MEDIUM,
    ReviewPriority.LOW: RuleSeverity.LOW,
}
REQUEST_CODE = "REVIEW_REQUESTED"


def request_item(request_id: str, priority: ReviewPriority, message: str) -> ReviewItem:
    """A review request (by a person or an investigation) as an item of the document's task."""
    return ReviewItem(
        key=_key("request", request_id),
        category="REQUESTED",
        code=REQUEST_CODE,
        severity=REQUEST_SEVERITY.get(priority, RuleSeverity.MEDIUM),
        message=message,
    )


def document_reasons(processing: Sequence[str], items: Sequence[ReviewItem]) -> list[str]:
    """`documents.review_reasons`: processing reasons plus what the rules found."""
    codes = [code for code in processing if code not in RULE_REASONS]
    if any(item.category == "DISCREPANCY" for item in items):
        codes.append(ReviewReason.RULE_VIOLATION.value)
    if any(item.category == "DUPLICATE" for item in items):
        codes.append(ReviewReason.DUPLICATE_SUSPECTED.value)
    return list(dict.fromkeys(codes))


def priority(items: Sequence[ReviewItem]) -> ReviewPriority:
    top = max(items, key=lambda item: SEVERITY_RANK[item.severity])
    return PRIORITY_OF[top.severity]


def task_type(items: Sequence[ReviewItem]) -> ReviewTaskType:
    present = {CATEGORY_TASK[item.category] for item in items}
    return next(kind for kind in _TASK_ORDER if kind in present)


def ordered(items: Sequence[ReviewItem]) -> list[ReviewItem]:
    """Most severe first, then by category importance."""
    order = {kind: index for index, kind in enumerate(_TASK_ORDER)}
    return sorted(
        items,
        key=lambda item: (-SEVERITY_RANK[item.severity], order[CATEGORY_TASK[item.category]]),
    )

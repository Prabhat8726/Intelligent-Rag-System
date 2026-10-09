"""Measured confidence for extracted values (Module 26) and the review routing it drives.

A field's confidence is a product of factors, each derived from a signal that is measured, not
self-reported by a model (ADR-005):

| signal        | factor                                                          |
|---------------|-----------------------------------------------------------------|
| evidence      | VERIFIED 1.0, FUZZY score/100, UNSUPPORTED 0.1, NOT_FOUND 0.0    |
| normalization | OK 1.0, UNCERTAIN 0.6 (e.g. ambiguous day/month), INVALID 0.0   |
| ocr           | native text 1.0, else 0.5 + 0.5 x mean word confidence / 100    |
| anchor        | layout rule strength (exact label 1.0 ... letterhead guess 0.75) |
| conflicts     | 0.8 when the same rule found different values                   |
| page          | 0.95 when the model cited the wrong page                        |
| consistency   | a check involving the field passed 1.0, all failed 0.6, none 1.0 |
| agreement     | two sources agree 1.0, single source 0.9, disagree 0.6           |
|               | (only the LLM read it: 0.8, below the default AUTO threshold)    |

Two sources are the layout extractor and the LLM, or a printed vendor name and the vendor
master. A value only the model found is printed on the page (evidence) but nothing shows it is
the right field - text on the page can talk a model into quoting it - so on its own it never
reaches AUTO with the default thresholds. The factor values are design choices recorded here;
whether they are well calibrated is measured by the evaluation (error rate inside the
auto-accepted bucket), not assumed.

Document confidence is the weakest required field (missing = 0). Routing: at or above HIGH and
no failed check -> AUTO; at or above MEDIUM -> ANALYST_REVIEW; otherwise MANDATORY_REVIEW.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from docintel.fields.evidence import EvidenceStatus
from docintel.fields.normalize import NormalizationStatus

CONFLICT_FACTOR = 0.8
WRONG_PAGE_FACTOR = 0.95
CONSISTENCY_FAIL_FACTOR = 0.6
AGREEMENT_FACTORS = {True: 1.0, None: 0.9, False: 0.6}
MODEL_ONLY_FACTOR = 0.8
NORMALIZATION_FACTORS = {
    NormalizationStatus.OK: 1.0,
    NormalizationStatus.UNCERTAIN: 0.6,
    NormalizationStatus.INVALID: 0.0,
}
UNSUPPORTED_FACTOR = 0.1


class ReviewLevel(StrEnum):
    AUTO = "AUTO"
    ANALYST_REVIEW = "ANALYST_REVIEW"
    MANDATORY_REVIEW = "MANDATORY_REVIEW"


@dataclass(frozen=True, slots=True)
class Thresholds:
    high: float = 0.85
    medium: float = 0.6


def evidence_factor(status: EvidenceStatus, score: float) -> float:
    match status:
        case EvidenceStatus.VERIFIED | EvidenceStatus.HUMAN:
            return 1.0
        case EvidenceStatus.FUZZY:
            return max(0.0, min(1.0, score / 100))
        case EvidenceStatus.UNSUPPORTED:
            return UNSUPPORTED_FACTOR
        case EvidenceStatus.NOT_FOUND:
            return 0.0


def ocr_factor(ocr_confidence: float | None) -> float:
    if ocr_confidence is None:
        return 1.0
    return 0.5 + 0.5 * max(0.0, min(100.0, ocr_confidence)) / 100


def _agreement_factor(signals: dict[str, Any]) -> float:
    agreement = signals.get("agreement")
    if agreement is None and signals.get("model_only"):
        return MODEL_ONLY_FACTOR
    return AGREEMENT_FACTORS[agreement]


def field_confidence(signals: dict[str, Any]) -> float:
    """Confidence in [0, 1] from a field's stored signals (recomputable after corrections)."""
    if signals.get("human"):
        return 1.0
    factors = (
        evidence_factor(
            EvidenceStatus(signals["evidence"]), float(signals.get("evidence_score", 0))
        ),
        NORMALIZATION_FACTORS[NormalizationStatus(signals["normalization"])],
        ocr_factor(signals.get("ocr")),
        float(signals.get("anchor", 1.0)),
        CONFLICT_FACTOR if signals.get("conflicts", 0) else 1.0,
        1.0 if signals.get("page_matches_citation", True) else WRONG_PAGE_FACTOR,
        CONSISTENCY_FAIL_FACTOR if signals.get("consistency") is False else 1.0,
        _agreement_factor(signals),
    )
    result = 1.0
    for factor in factors:
        result *= factor
    return round(result, 4)


def document_confidence(required: Iterable[float | None]) -> float:
    """Weakest required field; a missing one (None) counts as 0. No required fields: 1.0."""
    values = [0.0 if value is None else value for value in required]
    return round(min(values), 4) if values else 1.0


def review_level(confidence: float, *, failed_checks: bool, thresholds: Thresholds) -> ReviewLevel:
    if confidence >= thresholds.high and not failed_checks:
        return ReviewLevel.AUTO
    if confidence >= thresholds.medium:
        return ReviewLevel.ANALYST_REVIEW
    return ReviewLevel.MANDATORY_REVIEW

"""Calibration of extraction confidence (Phase 10; ADR-005 and ADR-068).

Confidence is a product of measured factors (`fields.confidence`), not fitted to data. Two
questions are measured here, on labelled synthetic documents:

* Is a field's confidence a probability? Reliability bins (mean confidence vs accuracy) and the
  expected calibration error (ECE). A low score that is usually right is under-confident: safe,
  but it sends correct documents to review.
* Where should the auto-accept threshold be? Documents are auto-accepted at or above the
  threshold when no consistency check failed (`review_level`). The sweep reports, per
  threshold, the auto-accepted share, the errors among them and a one-sided 95% upper bound on
  the error rate (Clopper-Pearson: zero errors in n documents is not a zero error rate).

The threshold is chosen on the development split by a rule fixed before looking at the held-out
split, which is then reported unchanged. The rule only considers thresholds above the design
floor: a value only a model read scores at most MODEL_ONLY_FACTOR (0.8), an uncertain date or a
disagreement 0.6, and those must never be auto-accepted whatever synthetic data allows.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from scipy.stats import beta

from docintel.fields.confidence import MODEL_ONLY_FACTOR

THRESHOLD_GRID = (0.5, 0.6, 0.7, 0.75, 0.8, 0.82, 0.85, 0.88, 0.9, 0.95)
DESIGN_FLOOR = MODEL_ONLY_FACTOR  # thresholds at or below it would auto-accept model-only values
BINS = 10
UPPER_BOUND_LEVEL = 0.95


@dataclass(frozen=True, slots=True)
class DocumentOutcome:
    confidence: float  # the document's overall confidence (weakest required value or cell)
    failed_checks: bool  # a consistency check failed: never auto-accepted
    any_error: bool  # a header field or line-item cell differs from the truth


def error_upper_bound(errors: int, n: int, level: float = UPPER_BOUND_LEVEL) -> float | None:
    """One-sided Clopper-Pearson upper bound on an error rate observed as errors / n."""
    if n == 0:
        return None
    if errors >= n:
        return 1.0
    return round(float(beta.ppf(level, errors + 1, n - errors)), 4)


def reliability(pairs: Iterable[tuple[float, bool]], bins: int = BINS) -> list[dict[str, float]]:
    """Equal-width confidence bins with their mean confidence and accuracy (empty bins left
    out). The top bin includes 1.0."""
    grouped: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in pairs:
        index = min(bins - 1, max(0, int(confidence * bins)))
        grouped[index].append((confidence, correct))
    rows = []
    for index, members in enumerate(grouped):
        if not members:
            continue
        rows.append(
            {
                "low": round(index / bins, 2),
                "high": round((index + 1) / bins, 2),
                "n": len(members),
                "mean_confidence": round(sum(c for c, _ in members) / len(members), 4),
                "accuracy": round(sum(ok for _, ok in members) / len(members), 4),
            }
        )
    return rows


def calibration_error(pairs: Sequence[tuple[float, bool]], bins: int = BINS) -> float | None:
    """Sum over bins of (share of values) x |accuracy - mean confidence|."""
    if not pairs:
        return None
    total = len(pairs)
    return round(
        sum(
            row["n"] / total * abs(row["accuracy"] - row["mean_confidence"])
            for row in reliability(pairs, bins)
        ),
        4,
    )


def overconfident_share(pairs: Sequence[tuple[float, bool]], bins: int = BINS) -> float | None:
    """Share of values in bins whose accuracy is below their mean confidence (the unsafe side)."""
    if not pairs:
        return None
    rows = reliability(pairs, bins)
    unsafe = sum(row["n"] for row in rows if row["accuracy"] < row["mean_confidence"])
    return round(unsafe / len(pairs), 4)


def threshold_sweep(
    documents: Sequence[DocumentOutcome], grid: Sequence[float] = THRESHOLD_GRID
) -> list[dict[str, float | int | None]]:
    rows: list[dict[str, float | int | None]] = []
    for threshold in grid:
        auto = [d for d in documents if d.confidence >= threshold and not d.failed_checks]
        errors = sum(d.any_error for d in auto)
        rows.append(
            {
                "threshold": threshold,
                "documents": len(documents),
                "auto": len(auto),
                "auto_share": round(len(auto) / len(documents), 4) if documents else None,
                "errors": errors,
                "error_rate": round(errors / len(auto), 4) if auto else None,
                "error_upper_95": error_upper_bound(errors, len(auto)),
            }
        )
    return rows


def choose_threshold(
    sweep: Sequence[dict[str, float | int | None]], floor: float = DESIGN_FLOOR
) -> float | None:
    """The rule, fixed in advance: the lowest threshold of the grid above the design floor at
    which neither it nor any higher threshold auto-accepts a document with an error on the
    development split. None when even the highest threshold lets an error through."""
    chosen = None
    candidates = [row for row in sweep if float(row["threshold"] or 0) > floor]
    for row in sorted(candidates, key=lambda r: float(r["threshold"] or 0), reverse=True):
        if row["errors"]:
            break
        chosen = row["threshold"]
    return float(chosen) if chosen is not None else None

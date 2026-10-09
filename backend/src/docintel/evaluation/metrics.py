"""Evaluation metrics (Module 25). Pure functions, unit-tested against hand-computed values."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean, median

from rapidfuzz.distance import Levenshtein


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate: edit distance / reference length (can exceed 1)."""
    if not reference:
        return 0.0 if not hypothesis else 1.0
    return Levenshtein.distance(reference, hypothesis) / len(reference)


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate over whitespace tokens."""
    ref_words, hyp_words = reference.split(), hypothesis.split()
    if not ref_words:
        return 0.0 if not hyp_words else 1.0
    return Levenshtein.distance(ref_words, hyp_words) / len(ref_words)


def bag_of_words_f1(reference: str, hypothesis: str) -> float:
    """Order-insensitive token F1 (robust to reading-order differences)."""
    ref, hyp = Counter(reference.split()), Counter(hypothesis.split())
    overlap = sum((ref & hyp).values())
    if not ref and not hyp:
        return 1.0
    precision = overlap / max(1, sum(hyp.values()))
    recall = overlap / max(1, sum(ref.values()))
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def counts_prf(tp: int, fp: int, fn: int) -> dict[str, int | float | None]:
    """Precision / recall / F1 from counts (None where undefined: nothing found or expected)."""
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": None if precision is None else round(precision, 4),
        "recall": None if recall is None else round(recall, 4),
        "f1": None if f1 is None else round(f1, 4),
    }


def summary(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0, "n": 0}
    return {
        "mean": round(mean(values), 4),
        "median": round(median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "n": len(values),
    }


@dataclass(frozen=True, slots=True)
class Prediction:
    truth: str
    label: str
    confidence: float


def classification_report(
    predictions: Sequence[Prediction], labels: Sequence[str], threshold: float
) -> dict[str, object]:
    """Accuracy, per-class and macro P/R/F1, confusion matrix, calibration and routing metrics."""
    total = len(predictions)
    correct = sum(1 for p in predictions if p.label == p.truth)
    per_class: dict[str, dict[str, float]] = {}
    for label in labels:
        tp = sum(1 for p in predictions if p.label == label and p.truth == label)
        fp = sum(1 for p in predictions if p.label == label and p.truth != label)
        fn = sum(1 for p in predictions if p.label != label and p.truth == label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        support = tp + fn
        per_class[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": support,
        }
    supported = [stats for stats in per_class.values() if stats["support"]]
    confusion = {
        truth: {
            label: sum(1 for p in predictions if p.truth == truth and p.label == label)
            for label in labels
        }
        for truth in labels
    }
    accepted = [p for p in predictions if p.confidence >= threshold]
    accepted_correct = sum(1 for p in accepted if p.label == p.truth)
    return {
        "n": total,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "macro_f1": round(mean(s["f1"] for s in supported), 4) if supported else 0.0,
        "per_class": per_class,
        "confusion_matrix": confusion,
        "ece": round(expected_calibration_error(predictions), 4),
        "routing": {
            "threshold": threshold,
            "auto_accepted_share": round(len(accepted) / total, 4) if total else 0.0,
            "error_rate_in_auto_bucket": round(1 - accepted_correct / len(accepted), 4)
            if accepted
            else None,
            "review_share": round(1 - len(accepted) / total, 4) if total else 0.0,
        },
    }


def expected_calibration_error(predictions: Sequence[Prediction], bins: int = 10) -> float:
    """ECE over equal-width confidence bins of the predicted label (top-label calibration)."""
    if not predictions:
        return 0.0
    error = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        members = [
            p
            for p in predictions
            if low < p.confidence <= high or (index == 0 and p.confidence == 0)
        ]
        if not members:
            continue
        accuracy = sum(1 for p in members if p.label == p.truth) / len(members)
        confidence = mean(p.confidence for p in members)
        error += len(members) / len(predictions) * abs(accuracy - confidence)
    return error

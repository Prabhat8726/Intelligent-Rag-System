"""Near-duplicate detection (Module 29) on extracted facts.

The upload already flags byte-identical files (same SHA-256). This finds the same business
document sent again as a different file - re-rendered, re-scanned or re-typed:

* SAME_VENDOR_AND_NUMBER - same vendor and same document number (strong);
* SAME_VENDOR_AMOUNT_AND_DATE - same vendor, same total and currency, dates within a window
  (possible: a vendor can legitimately bill the same amount twice).

The older document is the original; only the newer one is reported as a duplicate. Semantic
similarity of the text arrives with embeddings in Phase 6.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from docintel.matching.compare import same_vendor
from docintel.matching.facts import DocumentFacts


class DuplicateKind(StrEnum):
    SAME_FILE = "SAME_FILE"
    SAME_VENDOR_AND_NUMBER = "SAME_VENDOR_AND_NUMBER"
    SAME_VENDOR_AMOUNT_AND_DATE = "SAME_VENDOR_AMOUNT_AND_DATE"


STRONG = frozenset({DuplicateKind.SAME_FILE, DuplicateKind.SAME_VENDOR_AND_NUMBER})


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    kind: DuplicateKind
    document_id: str | None
    label: str
    created_at: datetime | None
    evidence: dict[str, Any]

    @property
    def strong(self) -> bool:
        return self.kind in STRONG

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "document_id": self.document_id,
            "label": self.label,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "evidence": self.evidence,
        }


def is_older(candidate: DocumentFacts, subject: DocumentFacts) -> bool:
    """Whether `candidate` came first (ties broken by id so exactly one of two is newer)."""
    if candidate.created_at and subject.created_at and candidate.created_at != subject.created_at:
        return candidate.created_at < subject.created_at
    return str(candidate.document_id) < str(subject.document_id)


def _summary(facts: DocumentFacts) -> dict[str, Any]:
    number = facts.number
    return {
        "document_id": str(facts.document_id) if facts.document_id else None,
        "label": facts.display_name,
        "number": number.display if number else None,
        "date": facts.document_date.isoformat() if facts.document_date else None,
        "total": format(facts.total.normalize(), "f") if facts.total is not None else None,
        "currency": facts.currency,
        "vendor": facts.vendor_name or (facts.vendor.display if facts.vendor else None),
    }


def find_duplicates(
    subject: DocumentFacts,
    candidates: Iterable[DocumentFacts],
    *,
    date_window_days: int = 7,
    match_amount_and_date: bool = True,
    vendor_similarity: float = 90.0,
) -> list[DuplicateMatch]:
    """Older documents of the same type that `subject` duplicates, strongest first."""
    found: list[DuplicateMatch] = []
    for other in candidates:
        if other.document_id == subject.document_id and subject.document_id is not None:
            continue
        if other.document_type != subject.document_type or not is_older(other, subject):
            continue
        if same_vendor(subject, other, vendor_similarity) is not True:
            continue
        kind: DuplicateKind | None = None
        if subject.number_key and subject.number_key == other.number_key:
            kind = DuplicateKind.SAME_VENDOR_AND_NUMBER
        elif (
            match_amount_and_date
            and subject.total is not None
            and subject.total == other.total
            and subject.currency == other.currency
            and subject.document_date is not None
            and other.document_date is not None
            and abs((subject.document_date - other.document_date).days) <= date_window_days
        ):
            kind = DuplicateKind.SAME_VENDOR_AMOUNT_AND_DATE
        if kind is None:
            continue
        found.append(
            DuplicateMatch(
                kind=kind,
                document_id=str(other.document_id) if other.document_id else None,
                label=other.label or other.display_name,
                created_at=other.created_at,
                evidence={"this": _summary(subject), "other": _summary(other)},
            )
        )
    found.sort(
        key=lambda match: (
            not match.strong,
            match.created_at.timestamp() if match.created_at else 0,
        )
    )
    return found

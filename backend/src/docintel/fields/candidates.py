"""What an extractor proposes before verification, normalization and scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from docintel.processing.content import BBox


class Origin(StrEnum):
    LOCAL = "LOCAL"  # deterministic layout extractor
    LLM = "LLM"  # language / vision model
    BOTH = "BOTH"  # both extractors produced the same value
    DERIVED = "DERIVED"  # computed from other printed values (e.g. currency from symbols)
    HUMAN = "HUMAN"  # entered or confirmed by a reviewer


@dataclass(slots=True)
class Candidate:
    """One proposed value. `raw_value` is the text as printed; `source_text` the quote that
    contains it (label and value), `page` where it is."""

    raw_value: str
    page: int | None
    source_text: str | None
    origin: Origin
    # Strength of the local anchor: 1.0 = exact label next to the value; lower for fuzzy
    # labels, values below the label, letterhead guesses or derived values. 1.0 for LLM output.
    anchor: float = 1.0
    # Known location (layout extractor): the candidate is its own evidence.
    bbox: BBox | None = None
    ocr_confidence: float | None = None
    # How the value was found (shown to reviewers), e.g. "label 'Invoice No.'".
    method: str | None = None
    # Other distinct values the same rule produced (ambiguity signal).
    conflicts: int = 0


@dataclass(slots=True)
class RowCandidate:
    page: int | None
    source_text: str | None
    cells: dict[str, Candidate] = field(default_factory=dict)
    bbox: BBox | None = None


@dataclass(slots=True)
class ExtractorOutput:
    scalars: dict[str, Candidate] = field(default_factory=dict)
    rows: list[RowCandidate] = field(default_factory=list)
    lists: dict[str, list[Candidate]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.scalars and not self.rows and not any(self.lists.values())

"""Evidence verification (Module 7): find a quoted source text on the page and return where.

The page is serialized in reading order (lines top to bottom, segments and words left to right)
with whitespace removed and case folded; every character maps back to its word, so a match
yields the word boxes and their OCR confidence. Exact containment is VERIFIED; otherwise the
best approximate alignment (RapidFuzz) above the threshold is FUZZY; anything else NOT_FOUND.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz import fuzz

from docintel.fields.normalize import squash
from docintel.processing.content import BBox, PageContent, Word

DEFAULT_FUZZY_THRESHOLD = 85.0
_MIN_FUZZY_LENGTH = 4  # very short quotes ("1", "pcs") only count when found exactly


class EvidenceStatus(StrEnum):
    VERIFIED = "VERIFIED"  # quote found verbatim (ignoring case and spacing)
    FUZZY = "FUZZY"  # close match (OCR noise, small transcription differences)
    UNSUPPORTED = "UNSUPPORTED"  # quote found, but the value cannot be read from it
    NOT_FOUND = "NOT_FOUND"  # quote not on the document
    HUMAN = "HUMAN"  # entered or confirmed by a reviewer


@dataclass(frozen=True, slots=True)
class Evidence:
    status: EvidenceStatus
    page: int | None
    score: float  # 0-100 (100 for exact)
    bbox: BBox | None
    ocr_confidence: float | None  # mean OCR confidence of the matched words; None = native
    matched_text: str | None
    page_matches_citation: bool = True

    @classmethod
    def not_found(cls) -> Evidence:
        return cls(EvidenceStatus.NOT_FOUND, None, 0.0, None, None, None)


class PageText:
    """One page in reading order, squashed, with a character -> word map."""

    def __init__(self, page: PageContent) -> None:
        self.page = page
        order = [index for line in page.lines for index in line.word_indices]
        seen = set(order)
        order += [index for index in range(len(page.words)) if index not in seen]
        chars: list[str] = []
        owners: list[int] = []
        for index in order:
            token = squash(page.words[index].text)
            chars.append(token)
            owners.extend([index] * len(token))
        self.text = "".join(chars)
        self.owners = owners

    def words_for(self, start: int, end: int) -> list[Word]:
        indices = dict.fromkeys(self.owners[start:end])
        return [self.page.words[index] for index in indices]

    def _cuts_word(self, position: int) -> bool:
        """True if `position` falls between two letters/digits of the same word."""
        if position <= 0 or position >= len(self.text):
            return False
        return (
            self.owners[position - 1] == self.owners[position]
            and self.text[position - 1].isalnum()
            and self.text[position].isalnum()
        )

    def find_all(self, needle: str) -> list[int]:
        """Start offsets of `needle` that begin and end on word boundaries ("10" is not found
        inside "2010")."""
        positions: list[int] = []
        start = self.text.find(needle)
        while start != -1:
            if not self._cuts_word(start) and not self._cuts_word(start + len(needle)):
                positions.append(start)
            start = self.text.find(needle, start + 1)
        return positions


def _evidence_from(
    page_text: PageText,
    start: int,
    end: int,
    status: EvidenceStatus,
    score: float,
    cited_page: int | None,
) -> Evidence:
    words = page_text.words_for(start, end)
    confidences = [
        (word.confidence, len(word.text)) for word in words if word.confidence is not None
    ]
    total = sum(weight for _, weight in confidences)
    ocr = sum(c * w for c, w in confidences) / total if total else None
    number = page_text.page.page_number
    return Evidence(
        status=status,
        page=number,
        score=round(score, 2),
        bbox=BBox.enclosing([word.bbox for word in words]) if words else None,
        ocr_confidence=None if ocr is None else round(ocr, 2),
        matched_text=" ".join(word.text for word in words),
        page_matches_citation=cited_page is None or cited_page == number,
    )


def _distance(box: BBox | None, near: BBox | None) -> float:
    if box is None or near is None:
        return 0.0
    dx = max(near.x0 - box.x1, box.x0 - near.x1, 0.0)
    dy = max(near.y0 - box.y1, box.y0 - near.y1, 0.0)
    return dx + dy


class EvidenceLocator:
    def __init__(
        self, pages: Sequence[PageContent], fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD
    ) -> None:
        self._pages = {page.page_number: PageText(page) for page in pages}
        self._threshold = fuzzy_threshold

    @property
    def page_numbers(self) -> list[int]:
        return sorted(self._pages)

    def page(self, number: int) -> PageContent | None:
        text = self._pages.get(number)
        return text.page if text else None

    def _search_order(self, cited_page: int | None) -> list[PageText]:
        pages = [self._pages[n] for n in sorted(self._pages)]
        if cited_page in self._pages:
            pages.sort(key=lambda page: page.page.page_number != cited_page)
        return pages

    def locate(
        self, source_text: str, cited_page: int | None = None, near: BBox | None = None
    ) -> Evidence:
        """Find `source_text`, preferring the cited page and (if given) the area `near`."""
        needle = squash(source_text)
        if not needle:
            return Evidence.not_found()
        for page_text in self._search_order(cited_page):
            positions = page_text.find_all(needle)
            if not positions:
                continue
            candidates = [
                _evidence_from(
                    page_text,
                    start,
                    start + len(needle),
                    EvidenceStatus.VERIFIED,
                    100.0,
                    cited_page,
                )
                for start in positions
            ]
            return min(candidates, key=lambda evidence: _distance(evidence.bbox, near))
        if len(needle) < _MIN_FUZZY_LENGTH:
            return Evidence.not_found()
        best: Evidence | None = None
        for page_text in self._search_order(cited_page):
            if not page_text.text:
                continue
            alignment = fuzz.partial_ratio_alignment(needle, page_text.text)
            if alignment is None or alignment.score < self._threshold:
                continue
            if best is None or alignment.score > best.score:
                best = _evidence_from(
                    page_text,
                    alignment.dest_start,
                    alignment.dest_end,
                    EvidenceStatus.FUZZY,
                    alignment.score,
                    cited_page,
                )
        return best or Evidence.not_found()

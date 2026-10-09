"""Split contract text into clauses and compare two versions clause by clause.

A clause starts at a numbered heading ("7. Limitation of Liability", "7.2 Cap", "Clause 7 -
Fees", "Article 3: Term"). Text before the first heading is the preamble; the signature block
("IN WITNESS WHEREOF", "Signed for and on behalf") is its own part. Clauses of two versions are
aligned by title (clauses get renumbered when one is inserted or removed), then by number, then
by text similarity; every pair is UNCHANGED or MODIFIED (with a word-level diff), and what is
left is ADDED or REMOVED.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from rapidfuzz import fuzz

# "Clause 7 - Fees" / "7.2 Cap" / "7. Fees" / "7) Fees"; a bare "7 Fees" is too often a
# wrapped sentence ("30 Days after ...") to count. Titles start with a capital letter.
_HEADING = re.compile(
    r"^\s*(?:"
    r"(?i:clause|section|article)\s+(?P<prefixed>\d{1,3}(?:\.\d{1,3}){0,3})[.):]?\s*[-\u2013:]?\s*"
    r"|(?P<nested>\d{1,3}(?:\.\d{1,3}){1,3})\.?\s+"
    r"|(?P<single>\d{1,3})[.)]\s+"
    r")(?P<title>[A-Z][^\n]{1,90})$"
)
_SIGNATURES = re.compile(r"^\s*(in witness whereof|signed for and on behalf)", re.IGNORECASE)
_WORD = re.compile(r"\S+")
_TITLE_SIMILARITY = 88.0
_TEXT_SIMILARITY = 70.0
_MAX_TITLE_WORDS = 10


class ClauseChange(StrEnum):
    UNCHANGED = "UNCHANGED"
    MODIFIED = "MODIFIED"
    ADDED = "ADDED"
    REMOVED = "REMOVED"


@dataclass(slots=True)
class Clause:
    key: str  # "7", "7.2", "preamble", "signatures"
    number: str | None
    title: str
    text: str  # body without the heading line
    page: int | None = None

    @property
    def title_key(self) -> str:
        return _normalize(self.title)

    @property
    def body_key(self) -> str:
        return _normalize(self.text)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "number": self.number,
            "title": self.title,
            "text": self.text,
            "page": self.page,
        }


@dataclass(slots=True)
class ClauseDiff:
    change: ClauseChange
    old: Clause | None
    new: Clause | None
    similarity: float = 100.0
    # Word-level operations for MODIFIED: [{"op": "equal|insert|delete|replace", "old", "new"}].
    operations: list[dict[str, str]] = field(default_factory=list)
    renumbered: bool = False

    @property
    def title(self) -> str:
        clause = self.new or self.old
        return clause.title if clause else ""

    def to_json(self) -> dict[str, Any]:
        return {
            "change": self.change.value,
            "title": self.title,
            "old": self.old.to_json() if self.old else None,
            "new": self.new.to_json() if self.new else None,
            "similarity": round(self.similarity, 1),
            "renumbered": self.renumbered,
            "operations": self.operations,
        }


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.sub(r"[^\w%€$£.,]", " ", folded).split())


def parse_heading(line: str) -> tuple[str, str] | None:
    match = _HEADING.match(line)
    if match is None:
        return None
    title = match.group("title").strip().rstrip(".")
    if len(title.split()) > _MAX_TITLE_WORDS or title.endswith(","):
        return None  # a numbered sentence, not a heading
    number = match.group("prefixed") or match.group("nested") or match.group("single")
    return number, title


def segment(pages: Sequence[str]) -> list[Clause]:
    """Clauses of a document from its page texts (in page order)."""
    clauses: list[Clause] = []
    current = Clause("preamble", None, "Preamble", "", 1)
    body: list[str] = []

    def close() -> None:
        current.text = "\n".join(line for line in body if line.strip()).strip()
        if current.text or current.number is not None:
            clauses.append(current)

    for page_number, page in enumerate(pages, start=1):
        for line in page.splitlines():
            if current.key != "signatures" and _SIGNATURES.match(line):
                close()
                current, body = Clause("signatures", None, "Signatures", "", page_number), [line]
                continue
            heading = parse_heading(line) if current.key != "signatures" else None
            if heading is not None:
                close()
                number, title = heading
                current, body = Clause(number, number, title, "", page_number), []
                continue
            body.append(line.strip())
    close()
    return clauses


def _operations(old: str, new: str) -> list[dict[str, str]]:
    old_words, new_words = _WORD.findall(old), _WORD.findall(new)
    matcher = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    return [
        {"op": tag, "old": " ".join(old_words[i1:i2]), "new": " ".join(new_words[j1:j2])}
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
    ]


def _pair(old: Clause, new: Clause) -> ClauseDiff:
    if old.body_key == new.body_key:
        return ClauseDiff(ClauseChange.UNCHANGED, old, new, renumbered=old.number != new.number)
    return ClauseDiff(
        ClauseChange.MODIFIED,
        old,
        new,
        similarity=fuzz.ratio(old.body_key, new.body_key),
        operations=_operations(old.text, new.text),
        renumbered=old.number != new.number,
    )


def compare_clauses(old: Sequence[Clause], new: Sequence[Clause]) -> list[ClauseDiff]:
    """Clause-by-clause differences, in the order of the new version (removed ones in place)."""
    pairs: dict[int, int] = {}  # new index -> old index
    free_old = set(range(len(old)))

    def take(new_index: int, old_index: int) -> None:
        pairs[new_index] = old_index
        free_old.discard(old_index)

    # 1. Same title (renumbering does not matter); 2. same number; 3. similar text.
    for index, clause in enumerate(new):
        best = max(
            (
                (fuzz.ratio(clause.title_key, old[i].title_key), i)
                for i in free_old
                if old[i].key not in ("preamble", "signatures") or old[i].key == clause.key
            ),
            default=None,
        )
        if best is not None and best[0] >= _TITLE_SIMILARITY:
            take(index, best[1])
    for index, clause in enumerate(new):
        if index in pairs or clause.number is None:
            continue
        twin = next((i for i in free_old if old[i].number == clause.number), None)
        if twin is not None and fuzz.ratio(clause.body_key, old[twin].body_key) >= _TEXT_SIMILARITY:
            take(index, twin)
    for index, clause in enumerate(new):
        if index in pairs:
            continue
        best = max(
            ((fuzz.ratio(clause.body_key, old[i].body_key), i) for i in free_old), default=None
        )
        if best is not None and best[0] >= _TEXT_SIMILARITY:
            take(index, best[1])

    result: list[ClauseDiff] = []
    emitted: set[int] = set()
    for index, clause in enumerate(new):
        if index in pairs:
            old_index = pairs[index]
            # Removed clauses that came before this one in the old version go here.
            for gone in sorted(i for i in free_old if i < old_index and i not in emitted):
                result.append(ClauseDiff(ClauseChange.REMOVED, old[gone], None))
                emitted.add(gone)
            result.append(_pair(old[old_index], clause))
        else:
            result.append(ClauseDiff(ClauseChange.ADDED, None, clause))
    for gone in sorted(i for i in free_old if i not in emitted):
        result.append(ClauseDiff(ClauseChange.REMOVED, old[gone], None))
    return result


def summarize(diffs: Sequence[ClauseDiff]) -> dict[str, int]:
    counts = {change.value: 0 for change in ClauseChange}
    for diff in diffs:
        counts[diff.change.value] += 1
    return counts

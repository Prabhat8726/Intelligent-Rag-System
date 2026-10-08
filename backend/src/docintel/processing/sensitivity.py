"""Content-based sensitivity assessment (input to the external-AI gate, Module 41).

Detects data that raises a document's sensitivity regardless of its label:
* payment card numbers (13-19 digits, Luhn-valid)        -> RESTRICTED
* US social security numbers (validated area/group/serial) -> RESTRICTED
* document types that are personal or confidential by nature (resume, bank statement)
                                                             -> CONFIDENTIAL
IBANs (mod-97 valid) and e-mail addresses are recorded as findings without raising the level:
they appear on ordinary business invoices. Only counts and page numbers are stored, never the
matched values.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from docintel.ai.routing import max_sensitivity
from docintel.db.models import DocumentType, Sensitivity

TYPE_MINIMUM: dict[DocumentType, Sensitivity] = {
    DocumentType.RESUME: Sensitivity.CONFIDENTIAL,
    DocumentType.BANK_STATEMENT: Sensitivity.CONFIDENTIAL,
}

_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_SSN = re.compile(r"(?<!\d)(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")


def luhn_valid(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def iban_valid(candidate: str) -> bool:
    compact = candidate.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    try:
        number = "".join(str(int(char, 36)) for char in rearranged)
    except ValueError:
        return False
    return int(number) % 97 == 1


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    count: int
    pages: list[int]
    level: Sensitivity | None  # None = informational, does not raise the level

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "count": self.count,
            "pages": self.pages,
            "level": self.level.value if self.level else None,
        }


@dataclass(frozen=True, slots=True)
class SensitivityAssessment:
    findings: list[Finding] = field(default_factory=list)
    type_minimum: Sensitivity | None = None

    @property
    def detected(self) -> Sensitivity | None:
        levels = [finding.level for finding in self.findings if finding.level is not None]
        if self.type_minimum is not None:
            levels.append(self.type_minimum)
        return max_sensitivity(*levels) if levels else None

    def with_type(self, document_type: DocumentType | None) -> SensitivityAssessment:
        minimum = TYPE_MINIMUM.get(document_type) if document_type else None
        return SensitivityAssessment(self.findings, minimum)

    def to_json(self) -> dict[str, object]:
        detected = self.detected
        return {
            "findings": [finding.to_json() for finding in self.findings],
            "type_minimum": self.type_minimum.value if self.type_minimum else None,
            "detected": detected.value if detected else None,
        }


def _cards(text: str) -> int:
    count = 0
    for match in _CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and len(set(digits)) > 1 and luhn_valid(digits):
            count += 1
    return count


def assess_pages(pages: Iterable[tuple[int, str]]) -> SensitivityAssessment:
    detectors = {
        "PAYMENT_CARD": (_cards, Sensitivity.RESTRICTED),
        "US_SSN": (lambda text: len(_SSN.findall(text)), Sensitivity.RESTRICTED),
        "IBAN": (lambda text: sum(1 for m in _IBAN.finditer(text) if iban_valid(m.group())), None),
        "EMAIL": (lambda text: len(_EMAIL.findall(text)), None),
    }
    counts: dict[str, tuple[int, list[int]]] = {}
    for page_number, text in pages:
        for kind, (detect, _) in detectors.items():
            found = detect(text)
            if found:
                total, page_list = counts.get(kind, (0, []))
                counts[kind] = (total + found, [*page_list, page_number])
    findings = [
        Finding(kind, total, page_list, detectors[kind][1])
        for kind, (total, page_list) in counts.items()
    ]
    return SensitivityAssessment(findings)

"""Natural-language search requests to structured filters plus free text (Module 28).

Deterministic and explainable: every recognized phrase becomes a filter and is reported back
("interpretation"), and whatever is not understood stays as free text for the full-text and
vector search. No model is involved, so a query never leaves the platform.

  "Find all invoices from Vendor Kestrel"            -> type INVOICE, vendor "Kestrel"
  "contracts containing termination clauses"          -> type CONTRACT, text "termination clauses"
  "documents with payment terms longer than 60 days"  -> payment_terms_days > 60
  "invoices over $10,000 in March 2026"               -> type INVOICE, total > 10000, March 2026
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from docintel.db.models import DocumentType

_TYPES: list[tuple[str, DocumentType]] = [
    (r"purchase[\s-]+orders?|\bpos\b|\bp\.o\.s?\b", DocumentType.PURCHASE_ORDER),
    (r"delivery\s+notes?|goods\s+received\s+notes?|packing\s+slips?", DocumentType.DELIVERY_NOTE),
    (r"bank\s+statements?", DocumentType.BANK_STATEMENT),
    (r"invoices?|\bbills\b", DocumentType.INVOICE),
    (r"receipts?", DocumentType.RECEIPT),
    (r"contracts?|agreements?", DocumentType.CONTRACT),
    (r"r[eé]sum[eé]s?|\bcvs?\b", DocumentType.RESUME),
    (r"insurance\s+polic(?:y|ies)", DocumentType.POLICY),
]
_GREATER = {"longer than", "more than", "greater than", "over", "above", "exceeding", ">"}
_GREATER_EQUAL = {"at least", ">=", "minimum of", "min"}
_LESS = {"shorter than", "less than", "under", "below", "<"}
_LESS_EQUAL = {"at most", "<=", "maximum of", "max", "up to"}
_OPERATORS = sorted([*_GREATER, *_GREATER_EQUAL, *_LESS, *_LESS_EQUAL], key=len, reverse=True)
_OP = "|".join(re.escape(op) for op in _OPERATORS)
_PAYMENT_TERMS = re.compile(
    rf"\b(?:with\s+|having\s+|mentioning\s+)?payment\s+terms?\s+(?:of\s+)?"
    rf"(?P<op>{_OP}|of|equal\s+to|=)?\s*(?P<n>\d{{1,3}})\s*(?:calendar\s+)?days?\b",
    re.IGNORECASE,
)
_NET = re.compile(r"\b(?:payment\s+terms?\s+)?net\s*(?P<n>\d{1,3})\b(?:\s*days?)?", re.IGNORECASE)
_AMOUNT = re.compile(
    rf"\b(?:with\s+)?(?:a\s+)?(?:total|amount|value|worth)?\s*(?:of\s+)?(?P<op>{_OP})\s*"
    r"(?P<cur>[$€£]|usd|eur|gbp)?\s*(?P<n>\d[\d,]*(?:\.\d+)?)\s*(?P<mult>k|thousand|m|million)?"
    r"\b(?!\s*(?:days?|%|percent))\s*(?:usd|eur|gbp|dollars|euros|pounds)?",
    re.IGNORECASE,
)
_ISO = r"\d{4}-\d{2}-\d{2}"
_BETWEEN = re.compile(rf"\bbetween\s+(?P<a>{_ISO})\s+and\s+(?P<b>{_ISO})\b", re.IGNORECASE)
_AFTER = re.compile(rf"\b(?P<word>after|since|from)\s+(?P<d>{_ISO})\b", re.IGNORECASE)
_BEFORE = re.compile(rf"\b(?P<word>before|until|through)\s+(?P<d>{_ISO})\b", re.IGNORECASE)
_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
_MONTH = re.compile(
    r"\b(?:in|during)\s+(?P<m>" + "|".join(_MONTHS) + r")\s+(?P<y>(?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(?:in|during)\s+(?P<y>(?:19|20)\d{2})\b", re.IGNORECASE)
_STOP = (
    r"with|containing|contains|mentioning|mention|about|regarding|that|which|where|whose|"
    r"over|above|under|below|more|less|greater|after|before|since|between|in|during|dated|"
    r"on|and|having|for|exceeding|at|up"
)
_VENDOR = re.compile(
    r"\b(?:from|by|issued\s+by|billed\s+by|sent\s+by|vendor|supplier)\s+"
    r"(?:the\s+)?(?:vendor\s+|supplier\s+)?"
    # "and"/"of" belong to a name when a capitalized word follows ("Harbor and Pine").
    r"(?:\"(?P<quoted>[^\"]{1,120})\"|(?P<name>[\w&.'-]+(?:\s+(?:(?:and|of)\s+(?=(?-i:[A-Z])))?(?!(?:"
    + _STOP
    + r")\b)[\w&.'-]+){0,6}))",
    re.IGNORECASE,
)
_FILLER = frozenset(
    [
        "and",
        "or",
        "find",
        "show",
        "list",
        "get",
        "give",
        "me",
        "all",
        "any",
        "every",
        "the",
        "a",
        "an",
        "documents",
        "document",
        "files",
        "file",
        "which",
        "that",
        "with",
        "containing",
        "contain",
        "contains",
        "mentioning",
        "mention",
        "mentions",
        "about",
        "where",
        "have",
        "has",
        "are",
        "is",
        "of",
        "for",
        "please",
        "our",
        "we",
        "us",
        "those",
        "these",
        "some",
        "search",
        "look",
        "looking",
        "up",
        "there",
    ]
)


@dataclass(frozen=True, slots=True)
class Comparison:
    op: str  # gt, gte, lt, lte, eq
    value: Decimal

    def matches(self, value: Decimal) -> bool:
        match self.op:
            case "gt":
                return value > self.value
            case "gte":
                return value >= self.value
            case "lt":
                return value < self.value
            case "lte":
                return value <= self.value
            case _:
                return value == self.value

    def describe(self, unit: str = "") -> str:
        words = {"gt": "more than", "gte": "at least", "lt": "less than", "lte": "at most"}
        number = f"{self.value:,}" if self.value == self.value.to_integral() else str(self.value)
        return f"{words.get(self.op, 'exactly')} {number}{unit}"


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    document_types: tuple[DocumentType, ...] = ()
    vendor: str | None = None
    payment_terms_days: Comparison | None = None
    total: Comparison | None = None
    date_from: date | None = None
    date_to: date | None = None
    text: str = ""
    recognized: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_filters(self) -> bool:
        return bool(
            self.document_types
            or self.vendor
            or self.payment_terms_days
            or self.total
            or self.date_from
            or self.date_to
        )


def _operator(word: str | None) -> str:
    phrase = " ".join((word or "").lower().split())
    if phrase in _GREATER:
        return "gt"
    if phrase in _GREATER_EQUAL:
        return "gte"
    if phrase in _LESS:
        return "lt"
    if phrase in _LESS_EQUAL:
        return "lte"
    return "eq"


def _remove(text: str, match: re.Match[str]) -> str:
    return text[: match.start()] + " " + text[match.end() :]


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_query(query: str) -> ParsedQuery:
    text = " ".join(query.split())
    recognized: list[str] = []
    date_from: date | None = None
    date_to: date | None = None

    # Dates first ("from 2026-01-01" is a date, not a vendor).
    if match := _BETWEEN.search(text):
        date_from, date_to = _date(match["a"]), _date(match["b"])
        text = _remove(text, match)
    for match in list(_AFTER.finditer(text))[:1]:
        found = _date(match["d"])
        if found is not None:
            date_from = found + timedelta(days=1) if match["word"].lower() == "after" else found
            text = _remove(text, match)
    for match in list(_BEFORE.finditer(text))[:1]:
        found = _date(match["d"])
        if found is not None:
            date_to = found - timedelta(days=1) if match["word"].lower() == "before" else found
            text = _remove(text, match)
    if match := _MONTH.search(text):
        year, month = int(match["y"]), _MONTHS[match["m"].lower()]
        date_from = date(year, month, 1)
        date_to = date(year, month, calendar.monthrange(year, month)[1])
        text = _remove(text, match)
    elif match := _YEAR.search(text):
        year = int(match["y"])
        date_from, date_to = date(year, 1, 1), date(year, 12, 31)
        text = _remove(text, match)
    if date_from or date_to:
        recognized.append(
            f"dated {date_from.isoformat() if date_from else '...'} to "
            f"{date_to.isoformat() if date_to else '...'}"
        )

    payment: Comparison | None = None
    if match := _PAYMENT_TERMS.search(text):
        payment = Comparison(_operator(match["op"]), Decimal(match["n"]))
        text = _remove(text, match)
    elif match := _NET.search(text):
        payment = Comparison("eq", Decimal(match["n"]))
        text = _remove(text, match)
    if payment is not None:
        recognized.append(f"payment terms {payment.describe(' days')}")

    total: Comparison | None = None
    if match := _AMOUNT.search(text):
        try:
            amount = Decimal(match["n"].replace(",", ""))
        except InvalidOperation:
            amount = None
        if amount is not None:
            multiplier = (match["mult"] or "").lower()
            if multiplier in {"k", "thousand"}:
                amount *= 1000
            elif multiplier in {"m", "million"}:
                amount *= 1_000_000
            total = Comparison(_operator(match["op"]), amount)
            text = _remove(text, match)
            recognized.append(f"total {total.describe()}")

    types: list[DocumentType] = []
    for pattern, document_type in _TYPES:
        type_match = re.search(pattern, text, re.IGNORECASE)
        if type_match is not None:
            if document_type not in types:
                types.append(document_type)
            text = _remove(text, type_match)
    if types:
        recognized.append("type " + " or ".join(t.value for t in types))

    vendor: str | None = None
    if match := _VENDOR.search(text):
        name = (match["quoted"] or match["name"] or "").strip(" .,'\"")
        if name and name.lower() not in _FILLER:
            vendor = name
            text = _remove(text, match)
            recognized.append(f"vendor {vendor!r}")

    words = [word for word in re.findall(r"[\w'-]+", text) if word.lower() not in _FILLER]
    return ParsedQuery(
        document_types=tuple(types),
        vendor=vendor,
        payment_terms_days=payment,
        total=total,
        date_from=date_from,
        date_to=date_to,
        text=" ".join(words),
        recognized=tuple(recognized),
    )

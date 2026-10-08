"""Normalization of printed values (Module 8): dates, amounts, currencies, percentages, periods,
identifiers and names. The printed `original_value` is always kept next to the result.

Ambiguity is reported, never guessed silently: `03/04/2026` is UNCERTAIN unless the document
itself (another date such as 13/04/2026) or its currency decides the order, and the reason is
recorded. Amounts are `Decimal`, never floats.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from rapidfuzz import fuzz

from docintel.fields.schemas import ValueType


class NormalizationStatus(StrEnum):
    OK = "OK"
    UNCERTAIN = "UNCERTAIN"  # parsed, but more than one reading is possible
    INVALID = "INVALID"  # does not parse as the declared type


class DateOrder(StrEnum):
    DMY = "DMY"
    MDY = "MDY"


@dataclass(frozen=True, slots=True)
class Normalized:
    value: Any  # JSON-serializable: str (Decimal, ISO date), int, bool, dict or None
    status: NormalizationStatus
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.status != NormalizationStatus.INVALID

    def to_json(self) -> dict[str, Any]:
        return {"value": self.value, "status": self.status.value, **self.detail}


@dataclass(frozen=True, slots=True)
class NormalizationContext:
    """Document-level hints that resolve ambiguous readings."""

    date_order: DateOrder | None = None
    date_order_reason: str | None = None
    currency: str | None = None
    decimal_comma: bool | None = None


# ------------------------------------------------------------------------------ text helpers
def collapse(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def squash(text: str) -> str:
    """Case- and whitespace-insensitive form used to compare quotes with page text."""
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def normalize_label(text: str) -> str:
    """Comparable form of a printed label: 'Invoice No.:' / 'Invoice #' -> 'invoice no'."""
    lowered = unicodedata.normalize("NFKC", text).casefold()
    lowered = re.sub(r"[#№º°]", " no ", lowered)
    lowered = re.sub(r"[^\w&%\s]", " ", lowered)
    tokens = [
        "no" if token in {"number", "num", "nr", "nbr"} else token for token in lowered.split()
    ]
    return " ".join(tokens)


# ------------------------------------------------------------------------------ currency
ISO_CURRENCIES = frozenset(
    {
        "USD", "EUR", "GBP", "INR", "JPY", "CHF", "CAD", "AUD", "CNY", "SEK", "NOK", "DKK",
        "PLN", "CZK", "HUF", "SGD", "HKD", "NZD", "ZAR", "AED", "SAR", "BRL", "MXN", "TRY",
    }
)  # fmt: skip
# Symbol -> (code, unambiguous). "$" and "¥" are shared by several currencies.
_SYMBOLS: tuple[tuple[str, str, bool], ...] = (
    ("us$", "USD", True),
    ("ca$", "CAD", True),
    ("c$", "CAD", True),
    ("a$", "AUD", True),
    ("au$", "AUD", True),
    ("€", "EUR", True),
    ("£", "GBP", True),
    ("₹", "INR", True),
    ("rs.", "INR", True),
    ("rs", "INR", True),
    ("¥", "JPY", False),
    ("$", "USD", False),
)
_DOLLAR_CURRENCIES = frozenset({"USD", "CAD", "AUD", "NZD", "SGD", "HKD", "MXN"})
_ISO_PATTERN = re.compile(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])")


def detect_currency(text: str, context_currency: str | None = None) -> tuple[str, bool] | None:
    """(ISO code, explicit) from a code or symbol in `text`; explicit=False when assumed."""
    for match in _ISO_PATTERN.finditer(text.upper()):
        if match.group(1) in ISO_CURRENCIES:
            return match.group(1), True
    lowered = text.casefold()
    for symbol, code, unambiguous in _SYMBOLS:
        if symbol.isalpha() or symbol.endswith("."):
            found = re.search(rf"(?<![a-z]){re.escape(symbol)}(?![a-z])", lowered) is not None
        else:
            found = symbol in lowered
        if not found:
            continue
        if unambiguous:
            return code, True
        if symbol == "$" and context_currency in _DOLLAR_CURRENCIES:
            return context_currency, True
        if symbol == "¥" and context_currency in {"JPY", "CNY"}:
            return context_currency, True
        return code, False
    return None


# ------------------------------------------------------------------------------ numbers
# Characters that group thousands but never mark decimals: space, apostrophe, right single
# quote (Swiss style), no-break and narrow no-break spaces.
_GROUPING = " '" + chr(0x2019) + chr(0xA0) + chr(0x202F)
_GROUP_CLASS = "[" + re.escape(_GROUPING) + "]"
# A spaced number must be in whole thousands groups ("12 345,67"); otherwise "6.81 68.10" (two
# table cells) would read as one number.
_NUMBER = re.compile(
    r"(?P<neg>(?<![\w.,])[-" + chr(0x2212) + r"]\s?)?(?P<open>\()?"
    r"(?P<num>\d{1,3}(?:" + _GROUP_CLASS + r"\d{3})+(?:[.,]\d+)?(?!\d)|\d(?:[\d.,]*\d)?)"
    r"(?P<close>\))?(?P<trail>-(?!\d))?"
)


@dataclass(frozen=True, slots=True)
class ParsedNumber:
    value: Decimal
    ambiguous: bool
    text: str
    percent: bool


def _decimal_from_digits(digits: str, decimal_comma: bool | None) -> tuple[Decimal, bool] | None:
    digits = re.sub(_GROUP_CLASS, "", digits)
    ambiguous = False
    decimal_mark: str | None
    if "," in digits and "." in digits:
        decimal_mark = "," if digits.rfind(",") > digits.rfind(".") else "."
    elif "," in digits or "." in digits:
        mark = "," if "," in digits else "."
        groups = digits.split(mark)
        if len(groups) > 2:
            decimal_mark = None  # repeated mark: thousands grouping ("1.234.567")
        elif len(groups[1]) != 3:
            decimal_mark = mark
        elif decimal_comma is None:
            # "1,234" / "1.234": a thousands group is far more likely than three decimals.
            decimal_mark, ambiguous = None, True
        else:
            decimal_mark = mark if decimal_comma == (mark == ",") else None
    else:
        decimal_mark = None
    if decimal_mark is None:
        plain = digits.replace(",", "").replace(".", "")
    else:
        thousands = "." if decimal_mark == "," else ","
        plain = digits.replace(thousands, "").replace(decimal_mark, ".")
    try:
        return Decimal(plain), ambiguous
    except InvalidOperation:
        return None


def find_numbers(text: str, decimal_comma: bool | None = None) -> list[ParsedNumber]:
    found: list[ParsedNumber] = []
    for match in _NUMBER.finditer(text):
        parsed = _decimal_from_digits(match.group("num"), decimal_comma)
        if parsed is None:
            continue
        value, ambiguous = parsed
        bracketed = match.group("open") is not None and match.group("close") is not None
        if match.group("neg") or match.group("trail") or bracketed:
            value = -value
        percent = text[match.end() :].lstrip().startswith("%")
        found.append(ParsedNumber(value, ambiguous, match.group(0).strip(), percent))
    return found


def parse_amount(text: str, decimal_comma: bool | None = None) -> ParsedNumber | None:
    """The last non-percentage number in `text` (labels such as 'VAT (20%)' come first)."""
    numbers = [number for number in find_numbers(text, decimal_comma) if not number.percent]
    return numbers[-1] if numbers else None


def parse_percent(text: str) -> Decimal | None:
    """'8.25%' -> Decimal('0.0825'): the rate as a fraction."""
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
    if match is None:
        return None
    return Decimal(match.group(1).replace(",", ".")) / 100


def decimal_text(value: Decimal) -> str:
    """Canonical decimal string without exponent or trailing zeros ('1200', '0.0825')."""
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


# ------------------------------------------------------------------------------ dates
MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "februar": 2, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "märz": 3, "maerz": 3, "april": 4, "apr": 4, "may": 5, "mai": 5, "june": 6, "jun": 6,
    "juni": 6, "july": 7, "jul": 7, "juli": 7, "august": 8, "aug": 8, "september": 9,
    "sep": 9, "sept": 9, "october": 10, "oct": 10, "oktober": 10, "okt": 10, "november": 11,
    "nov": 11, "december": 12, "dec": 12, "dezember": 12, "dez": 12, "januar": 1,
    "jänner": 1,
}  # fmt: skip
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_NUMERIC_DATE = re.compile(r"(?<![\d.])(\d{1,2})([./-])(\d{1,2})\2(\d{4}|\d{2})(?![\d])")
_DAY_MONTH_YEAR = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\.?[\s-]*(?:of\s+)?({_MONTH_NAMES})\b\.?[\s,-]*(\d{{4}}|\d{{2}})(?!\d)",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR = re.compile(
    rf"(?<![A-Za-z])({_MONTH_NAMES})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)",
    re.IGNORECASE,
)
_TWO_DIGIT_YEAR_PIVOT = 70


def _year(text: str) -> int:
    value = int(text)
    if len(text) == 2:
        return 2000 + value if value < _TWO_DIGIT_YEAR_PIVOT else 1900 + value
    return value


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class DateReading:
    value: date | None
    alternatives: tuple[date, ...]
    rule: str  # how the reading was decided
    span: tuple[int, int]

    @property
    def ambiguous(self) -> bool:
        return self.value is None and len(self.alternatives) > 1


def find_dates(text: str, context: NormalizationContext | None = None) -> list[DateReading]:
    """Every date printed in `text`, in order. Numeric day/month order follows the document
    context; without one, `03/04/2026` yields both readings and no value."""
    context = context or NormalizationContext()
    readings: list[DateReading] = []
    taken: list[tuple[int, int]] = []

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < end and start < span[1] for start, end in taken)

    for match in _ISO_DATE.finditer(text):
        iso = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if iso is not None:
            readings.append(DateReading(iso, (iso,), "year-month-day", match.span()))
            taken.append(match.span())
    for pattern, order in ((_DAY_MONTH_YEAR, "dmy"), (_MONTH_DAY_YEAR, "mdy")):
        for match in pattern.finditer(text):
            if overlaps(match.span()):
                continue
            if order == "dmy":
                day, month_name, year = match.group(1), match.group(2), match.group(3)
            else:
                month_name, day, year = match.group(1), match.group(2), match.group(3)
            named = _safe_date(_year(year), MONTHS[month_name.casefold()], int(day))
            if named is not None:
                readings.append(DateReading(named, (named,), "month name", match.span()))
                taken.append(match.span())
    for match in _NUMERIC_DATE.finditer(text):
        if overlaps(match.span()):
            continue
        first, separator, second, year_text = match.groups()
        year = _year(year_text)
        dmy = _safe_date(year, int(second), int(first))
        mdy = _safe_date(year, int(first), int(second))
        if dmy is None and mdy is None:
            continue
        value: date | None
        if dmy is None or mdy is None or dmy == mdy:
            value, rule = dmy or mdy, "only one valid reading"
        elif separator == ".":
            value, rule = dmy, "dotted dates are day-first"
        elif context.date_order is not None:
            value = dmy if context.date_order == DateOrder.DMY else mdy
            rule = f"document order {context.date_order.value}: {context.date_order_reason}"
        else:
            value, rule = None, "day/month order unknown"
        alternatives = (value,) if value else tuple(day for day in (dmy, mdy) if day is not None)
        readings.append(DateReading(value, alternatives, rule, match.span()))
        taken.append(match.span())
    return sorted(readings, key=lambda reading: reading.span)


def infer_date_order(texts: list[str], currency: str | None) -> tuple[DateOrder | None, str | None]:
    """Decide day/month order for a whole document from unambiguous dates, else its currency."""
    votes: dict[DateOrder, str] = {}
    for text in texts:
        for match in _NUMERIC_DATE.finditer(text):
            first, separator, second, _ = match.groups()
            if separator == ".":
                continue
            if int(first) > 12 >= int(second):
                votes.setdefault(DateOrder.DMY, match.group(0))
            elif int(second) > 12 >= int(first):
                votes.setdefault(DateOrder.MDY, match.group(0))
    if len(votes) == 1:
        order, example = next(iter(votes.items()))
        return order, f"'{example}' on the document can only be {order.value}"
    if len(votes) > 1 or currency is None:
        return None, None
    if currency == "USD":
        return DateOrder.MDY, "US dollar document"
    if currency in {"CAD", "CNY", "JPY"}:
        return None, None  # mixed or year-first conventions
    return DateOrder.DMY, f"{currency} document"


# ------------------------------------------------------------------------------ names, ids
_LEGAL_SUFFIXES = frozenset(
    {
        "inc", "incorporated", "llc", "l l c", "ltd", "limited", "gmbh", "g m b h", "pvt",
        "private", "co", "corp", "corporation", "company", "plc", "ag", "sa", "s a", "bv", "nv",
        "oy", "ab", "srl", "spa", "llp", "lp", "kg", "ohg", "pty", "sarl", "se",
    }
)  # fmt: skip


def organization_key(name: str) -> str:
    """Comparable form of a company name: casefolded, '&' -> 'and', punctuation and legal
    suffixes removed ('Kestrel Industrial Supply, Inc.' -> 'kestrel industrial supply')."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"\b([a-z])\.(?=[a-z]\.)", r"\1", text)  # "g.m.b.h." -> "gmbh."
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = text.split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    joined = " ".join(tokens)
    for suffix in ("l l c", "g m b h", "s a"):
        joined = joined.removesuffix(f" {suffix}")
    return joined.strip()


def name_similarity(a: str, b: str) -> float:
    """0-100 similarity of two organization keys (word order and abbreviations tolerated)."""
    if not a or not b:
        return 0.0
    return max(fuzz.token_set_ratio(a, b), fuzz.ratio(a, b))


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{5,}\d")
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14, "fifteen": 15,
    "thirty": 30, "sixty": 60, "ninety": 90,
}  # fmt: skip
_DAYS = re.compile(
    rf"(\d+|{'|'.join(_WORD_NUMBERS)})(?:\s*\(\d+\))?[\s-]*(calendar |business |working )?"
    r"(days?|tage?n?|weeks?|months?)?",
    re.IGNORECASE,
)
_IMMEDIATE = re.compile(r"\b(due on receipt|immediate(ly)?|upon receipt|sofort)\b", re.IGNORECASE)
_BOOLEAN_TRUE = re.compile(
    r"\b(automatically renew|auto[- ]?renew|shall renew|will renew|renews automatically|yes|true)",
    re.IGNORECASE,
)
_BOOLEAN_FALSE = re.compile(
    r"\b(not (?:be )?(?:automatically )?renew|no automatic renewal|shall not renew|no|false)\b",
    re.IGNORECASE,
)


def parse_days(text: str) -> tuple[int, dict[str, Any]] | None:
    if _IMMEDIATE.search(text):
        return 0, {"rule": "due immediately"}
    match = _DAYS.search(text)
    if match is None:
        return None
    raw, unit = match.group(1).casefold(), (match.group(3) or "days").casefold()
    count = int(raw) if raw.isdigit() else _WORD_NUMBERS[raw]
    if unit.startswith("week"):
        return count * 7, {"rule": "weeks x 7"}
    if unit.startswith("month"):
        return count * 30, {"rule": "months x 30 (approximate)", "months": count}
    return count, {}


# ------------------------------------------------------------------------------ dispatcher
def _invalid(reason: str) -> Normalized:
    return Normalized(None, NormalizationStatus.INVALID, {"reason": reason})


def normalize_value(
    value_type: ValueType, text: str, context: NormalizationContext | None = None
) -> Normalized:
    """Typed, canonical form of a printed value."""
    context = context or NormalizationContext()
    text = collapse(text)
    if not text:
        return _invalid("empty")
    match value_type:
        case ValueType.MONEY:
            number = parse_amount(text, context.decimal_comma)
            if number is None:
                return _invalid("no amount found")
            detected = detect_currency(text, context.currency)
            currency = detected[0] if detected else context.currency
            detail: dict[str, Any] = {"currency": currency}
            if detected is None and currency is not None:
                detail["currency_from"] = "document"
            elif detected is not None and not detected[1]:
                detail["currency_from"] = "symbol (assumed)"
            if number.ambiguous:
                detail["note"] = "digit grouping ambiguous; read as thousands"
            status = NormalizationStatus.UNCERTAIN if number.ambiguous else NormalizationStatus.OK
            return Normalized(decimal_text(number.value), status, detail)
        case ValueType.QUANTITY:
            numbers = find_numbers(text, context.decimal_comma)
            if not numbers:
                return _invalid("no number found")
            number = numbers[0]
            status = NormalizationStatus.UNCERTAIN if number.ambiguous else NormalizationStatus.OK
            return Normalized(decimal_text(number.value), status)
        case ValueType.INTEGER:
            match_int = re.search(r"\d+", text)
            if match_int is None:
                return _invalid("no integer found")
            return Normalized(int(match_int.group(0)), NormalizationStatus.OK)
        case ValueType.PERCENT:
            rate = parse_percent(text)
            if rate is None:
                return _invalid("no percentage found")
            return Normalized(decimal_text(rate), NormalizationStatus.OK)
        case ValueType.DAYS:
            parsed = parse_days(text)
            if parsed is None:
                return _invalid("no period found")
            return Normalized(parsed[0], NormalizationStatus.OK, parsed[1])
        case ValueType.DATE:
            readings = find_dates(text, context)
            if not readings:
                return _invalid("no date found")
            reading = readings[0]
            if reading.value is None:
                return Normalized(
                    None,
                    NormalizationStatus.UNCERTAIN,
                    {
                        "rule": reading.rule,
                        "alternatives": [day.isoformat() for day in reading.alternatives],
                    },
                )
            return Normalized(
                reading.value.isoformat(), NormalizationStatus.OK, {"rule": reading.rule}
            )
        case ValueType.CURRENCY:
            detected = detect_currency(text, context.currency)
            if detected is None:
                return _invalid("no currency code or symbol")
            status = NormalizationStatus.OK if detected[1] else NormalizationStatus.UNCERTAIN
            return Normalized(detected[0], status)
        case ValueType.IDENTIFIER:
            cleaned = text.strip(" :;,.")
            if not re.search(r"[A-Za-z0-9]", cleaned):
                return _invalid("no identifier characters")
            return Normalized(re.sub(r"\s+", "", cleaned).upper(), NormalizationStatus.OK)
        case ValueType.ORGANIZATION:
            key = organization_key(text)
            if not key:
                return _invalid("no name")
            return Normalized(text.strip(" ,;:"), NormalizationStatus.OK, {"key": key})
        case ValueType.PERSON | ValueType.TEXT:
            cleaned = text.strip(" ,;:")
            if not re.search(r"\w", cleaned):
                return _invalid("no text")
            return Normalized(cleaned, NormalizationStatus.OK)
        case ValueType.EMAIL:
            email = _EMAIL.search(text)
            if email is None:
                return _invalid("not an e-mail address")
            return Normalized(email.group(0).lower(), NormalizationStatus.OK)
        case ValueType.PHONE:
            phone = _PHONE.search(text)
            printed = phone.group(0) if phone else ""
            digits = re.sub(r"\D", "", printed)
            if not 7 <= len(digits) <= 15:
                return _invalid("not a phone number")
            prefix = "+" if printed.startswith("+") else ""
            return Normalized(prefix + digits, NormalizationStatus.OK)
        case ValueType.BOOLEAN:
            if _BOOLEAN_FALSE.search(text):
                return Normalized(False, NormalizationStatus.OK)
            if _BOOLEAN_TRUE.search(text):
                return Normalized(True, NormalizationStatus.OK)
            return Normalized(None, NormalizationStatus.UNCERTAIN, {"reason": "no yes/no reading"})


def comparable(value_type: ValueType, normalized: Any) -> Any:
    """Key used to decide whether two extractors agree on a value."""
    if normalized is None:
        return None
    if value_type in (ValueType.MONEY, ValueType.QUANTITY, ValueType.PERCENT):
        return Decimal(str(normalized))
    if value_type in (ValueType.ORGANIZATION, ValueType.PERSON, ValueType.TEXT):
        return (
            organization_key(str(normalized))
            if value_type == ValueType.ORGANIZATION
            else squash(str(normalized))
        )
    return normalized


def value_in_source(
    value_type: ValueType, value: str, source_text: str, context: NormalizationContext
) -> bool:
    """Can the value be read from its quoted source? (A model may quote correctly and still
    transcribe the value wrongly; this catches that.)"""
    if not source_text.strip():
        return False
    if squash(value) and squash(value) in squash(source_text):
        return True
    match value_type:
        case ValueType.MONEY | ValueType.QUANTITY | ValueType.INTEGER:
            target = normalize_value(value_type, value, context)
            if not target.valid:
                return False
            wanted = Decimal(str(target.value))
            return any(
                abs(number.value) == abs(wanted)
                for number in find_numbers(source_text, context.decimal_comma)
            )
        case ValueType.PERCENT:
            wanted_rate = parse_percent(value)
            return wanted_rate is not None and wanted_rate == parse_percent(source_text)
        case ValueType.DATE:
            wanted_date = normalize_value(ValueType.DATE, value, context)
            readings = find_dates(source_text, context)
            return wanted_date.value is not None and any(
                reading.value is not None and reading.value.isoformat() == wanted_date.value
                for reading in readings
            )
        case ValueType.DAYS:
            wanted_days = parse_days(value)
            found = parse_days(source_text)
            return wanted_days is not None and found is not None and wanted_days[0] == found[0]
        case ValueType.CURRENCY:
            wanted_code = detect_currency(value)
            found_code = detect_currency(source_text)
            return (
                wanted_code is not None
                and found_code is not None
                and wanted_code[0] == found_code[0]
            )
        case ValueType.BOOLEAN:
            return True  # a yes/no is read from the quoted clause itself
        case _:
            return fuzz.partial_ratio(squash(value), squash(source_text)) >= 90

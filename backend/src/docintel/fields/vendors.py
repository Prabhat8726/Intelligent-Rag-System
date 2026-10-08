"""Vendor resolution (Module 8): match a printed vendor name / tax ID to the vendor master.

Order of evidence: an exact tax ID is decisive; an exact alias (a spelling a reviewer already
confirmed) next; otherwise name similarity on organization keys (legal suffixes and punctuation
removed) above a threshold. Matching never creates vendors: unknown names stay unmatched.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import any_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.db.models import Vendor
from docintel.fields.normalize import name_similarity, organization_key

DEFAULT_MIN_SCORE = 85.0
_CANDIDATE_LIMIT = 25
_TRIGRAM_FLOOR = 0.2


def tax_id_key(value: str | None) -> str | None:
    if not value:
        return None
    key = re.sub(r"[^0-9A-Za-z]", "", value).upper()
    return key or None


@dataclass(frozen=True, slots=True)
class VendorRecord:
    id: uuid.UUID
    canonical_name: str
    name_key: str
    alias_keys: tuple[str, ...] = ()
    tax_id_key: str | None = None
    default_currency: str | None = None
    payment_terms_days: int | None = None

    @classmethod
    def from_model(cls, vendor: Vendor) -> VendorRecord:
        return cls(
            id=vendor.id,
            canonical_name=vendor.canonical_name,
            name_key=vendor.name_key,
            alias_keys=tuple(vendor.alias_keys or ()),
            tax_id_key=vendor.tax_id_key,
            default_currency=vendor.default_currency,
            payment_terms_days=vendor.payment_terms_days,
        )


@dataclass(frozen=True, slots=True)
class VendorMatch:
    vendor_id: uuid.UUID
    canonical_name: str
    score: float
    method: str  # tax_id | alias | name
    default_currency: str | None
    payment_terms_days: int | None

    def to_json(self) -> dict[str, object]:
        return {
            "vendor_id": str(self.vendor_id),
            "canonical_name": self.canonical_name,
            "score": round(self.score, 1),
            "method": self.method,
        }


def best_match(
    records: Iterable[VendorRecord],
    name: str | None,
    tax_id: str | None,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
) -> VendorMatch | None:
    records = list(records)
    wanted_tax = tax_id_key(tax_id)
    if wanted_tax:
        for record in records:
            if record.tax_id_key and record.tax_id_key == wanted_tax:
                return _match(record, 100.0, "tax_id")
    key = organization_key(name) if name else ""
    if not key:
        return None
    for record in records:
        if key == record.name_key or key in record.alias_keys:
            return _match(record, 100.0, "alias" if key != record.name_key else "name")
    scored = [(name_similarity(key, record.name_key), record) for record in records]
    scored += [
        (name_similarity(key, alias), record) for record in records for alias in record.alias_keys
    ]
    if not scored:
        return None
    score, record = max(scored, key=lambda item: item[0])
    return _match(record, score, "name") if score >= min_score else None


def _match(record: VendorRecord, score: float, method: str) -> VendorMatch:
    return VendorMatch(
        record.id,
        record.canonical_name,
        score,
        method,
        record.default_currency,
        record.payment_terms_days,
    )


class VendorDirectory(Protocol):
    async def match(self, name: str | None, tax_id: str | None) -> VendorMatch | None: ...

    async def has_vendors(self) -> bool: ...


class StaticVendorDirectory:
    """In-memory directory (tests, evaluation)."""

    def __init__(self, records: Iterable[VendorRecord] = (), min_score: float = DEFAULT_MIN_SCORE):
        self._records = list(records)
        self._min_score = min_score

    async def match(self, name: str | None, tax_id: str | None) -> VendorMatch | None:
        return best_match(self._records, name, tax_id, min_score=self._min_score)

    async def has_vendors(self) -> bool:
        return bool(self._records)


async def match_in_session(
    session: AsyncSession,
    name: str | None,
    tax_id: str | None,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
) -> VendorMatch | None:
    """Candidates from PostgreSQL (tax ID, alias, trigram similarity), scored in Python."""
    key = organization_key(name) if name else ""
    wanted_tax = tax_id_key(tax_id)
    conditions = []
    if wanted_tax:
        conditions.append(Vendor.tax_id_key == wanted_tax)
    if key:
        conditions.append(literal(key) == any_(Vendor.alias_keys))
        conditions.append(func.similarity(Vendor.name_key, literal(key)) >= _TRIGRAM_FLOOR)
    if not conditions:
        return None
    rows = await session.scalars(
        select(Vendor)
        .where(Vendor.is_active, or_(*conditions))
        .order_by(func.similarity(Vendor.name_key, literal(key)).desc())
        .limit(_CANDIDATE_LIMIT)
    )
    records = [VendorRecord.from_model(vendor) for vendor in rows]
    return best_match(records, name, tax_id, min_score=min_score)


class DatabaseVendorDirectory:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        min_score: float = DEFAULT_MIN_SCORE,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._min_score = min_score

    async def has_vendors(self) -> bool:
        async with self._sessionmaker() as session:
            found = await session.scalar(select(Vendor.id).where(Vendor.is_active).limit(1))
        return found is not None

    async def match(self, name: str | None, tax_id: str | None) -> VendorMatch | None:
        async with self._sessionmaker() as session:
            return await match_in_session(session, name, tax_id, min_score=self._min_score)

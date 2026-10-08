"""Vendor master data use-cases: list/search, create, update (audited)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.core.errors import ConflictError, NotFoundError, UnprocessableContentError
from docintel.db.models import AuditOutcome, User, Vendor
from docintel.fields.normalize import organization_key
from docintel.fields.vendors import tax_id_key

NOT_FOUND = "Vendor not found."
_SEARCH_SIMILARITY = 0.2


@dataclass(frozen=True, slots=True)
class VendorData:
    canonical_name: str
    aliases: Sequence[str] = ()
    tax_id: str | None = None
    default_currency: str | None = None
    payment_terms_days: int | None = None
    is_active: bool = True


def _keys(name: str, aliases: Sequence[str]) -> tuple[str, list[str]]:
    key = organization_key(name)
    if not key:
        msg = "The vendor name has no letters or digits after normalization."
        raise UnprocessableContentError(msg)
    alias_keys = sorted({k for alias in aliases if (k := organization_key(alias)) and k != key})
    return key, alias_keys


def apply_vendor_data(vendor: Vendor, data: VendorData) -> None:
    name = " ".join(data.canonical_name.split())
    aliases = sorted({" ".join(alias.split()) for alias in data.aliases if alias.strip()})
    vendor.canonical_name = name
    vendor.name_key, vendor.alias_keys = _keys(name, aliases)
    vendor.aliases = aliases
    vendor.tax_id = data.tax_id.strip() if data.tax_id and data.tax_id.strip() else None
    vendor.tax_id_key = tax_id_key(vendor.tax_id)
    vendor.default_currency = data.default_currency
    vendor.payment_terms_days = data.payment_terms_days
    vendor.is_active = data.is_active


class VendorService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _search(self, query: str | None) -> Select[Vendor]:
        statement = select(Vendor)
        if query:
            key = organization_key(query)
            statement = statement.where(
                or_(
                    Vendor.canonical_name.ilike(f"%{_escape_like(query)}%", escape="\\"),
                    func.similarity(Vendor.name_key, literal(key)) >= _SEARCH_SIMILARITY,
                    Vendor.tax_id_key == tax_id_key(query),
                )
            )
        return statement

    async def list(self, query: str | None, *, limit: int, offset: int) -> tuple[list[Vendor], int]:
        statement = self._search(query)
        total = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self._session.scalars(
            statement.order_by(Vendor.canonical_name).limit(limit).offset(offset)
        )
        return list(rows), int(total or 0)

    async def get(self, vendor_id: uuid.UUID) -> Vendor:
        vendor = await self._session.get(Vendor, vendor_id)
        if vendor is None:
            raise NotFoundError(NOT_FOUND)
        return vendor

    async def create(self, actor: User | None, data: VendorData, meta: RequestMeta) -> Vendor:
        vendor = Vendor(created_by_id=actor.id if actor else None)
        apply_vendor_data(vendor, data)
        self._session.add(vendor)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            msg = "A vendor with this name already exists."
            raise ConflictError(msg) from exc
        record_audit_event(
            self._session,
            action=AuditAction.VENDOR_CREATED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="vendor",
            entity_id=vendor.id,
            details={"canonical_name": vendor.canonical_name, "tax_id": bool(vendor.tax_id)},
        )
        await self._session.commit()
        await self._session.refresh(vendor)
        return vendor

    async def update(
        self, actor: User, vendor_id: uuid.UUID, data: VendorData, meta: RequestMeta
    ) -> Vendor:
        vendor = await self.get(vendor_id)
        before = {
            "canonical_name": vendor.canonical_name,
            "aliases": list(vendor.aliases),
            "is_active": vendor.is_active,
        }
        apply_vendor_data(vendor, data)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            msg = "A vendor with this name already exists."
            raise ConflictError(msg) from exc
        record_audit_event(
            self._session,
            action=AuditAction.VENDOR_UPDATED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="vendor",
            entity_id=vendor.id,
            details={
                "before": before,
                "after": {
                    "canonical_name": vendor.canonical_name,
                    "aliases": list(vendor.aliases),
                    "is_active": vendor.is_active,
                },
            },
        )
        await self._session.commit()
        await self._session.refresh(vendor)
        return vendor


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

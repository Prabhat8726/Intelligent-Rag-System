"""Comparisons API: automatic comparisons (built by matching) and ones users ask for."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.policies import visible_documents
from docintel.core.config import Settings
from docintel.core.errors import NotFoundError, UnprocessableContentError
from docintel.db.models import (
    AuditOutcome,
    Comparison,
    ComparisonDocument,
    ComparisonItemStatus,
    ComparisonOrigin,
    ComparisonRole,
    ComparisonType,
    Document,
    DocumentType,
    User,
)
from docintel.matching.compare import compare_delivery, compare_invoice
from docintel.matching.facts import DocumentFacts
from docintel.matching.service import PROCESSED, store_comparison
from docintel.matching.store import load_facts, load_rules
from docintel.rules.defaults import tolerances_from_rules

NOT_FOUND = "Comparison not found."
_ROLE_TYPE = {
    ComparisonRole.INVOICE: DocumentType.INVOICE,
    ComparisonRole.PURCHASE_ORDER: DocumentType.PURCHASE_ORDER,
    ComparisonRole.DELIVERY_NOTE: DocumentType.DELIVERY_NOTE,
}


@dataclass(slots=True)
class ComparisonFilters:
    document_id: uuid.UUID | None = None
    comparison_type: ComparisonType | None = None
    origin: ComparisonOrigin | None = None
    with_issues: bool = False


def _all_visible(actor: User) -> Any:
    """Every document of the comparison is visible to the actor (nothing leaks through it)."""
    return ~exists(
        select(ComparisonDocument.document_id)
        .join(Document, Document.id == ComparisonDocument.document_id)
        .where(
            ComparisonDocument.comparison_id == Comparison.id,
            ~visible_documents(actor),
        )
    )


class ComparisonService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def comparisons(
        self, actor: User, filters: ComparisonFilters, *, limit: int, offset: int
    ) -> tuple[list[Comparison], int]:
        conditions = [_all_visible(actor)]
        if filters.document_id is not None:
            conditions.append(
                exists(
                    select(ComparisonDocument.document_id).where(
                        ComparisonDocument.comparison_id == Comparison.id,
                        ComparisonDocument.document_id == filters.document_id,
                    )
                )
            )
        if filters.comparison_type is not None:
            conditions.append(Comparison.comparison_type == filters.comparison_type)
        if filters.origin is not None:
            conditions.append(Comparison.origin == filters.origin)
        if filters.with_issues:
            issues = (
                Comparison.summary["MISMATCH"].as_integer()
                + Comparison.summary["MISSING"].as_integer()
                + Comparison.summary["UNCERTAIN"].as_integer()
            )
            conditions.append(issues > 0)
        where = and_(*conditions)
        total = await self._session.scalar(
            select(func.count()).select_from(Comparison).where(where)
        )
        rows = await self._session.scalars(
            select(Comparison)
            .where(where)
            .order_by(Comparison.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def get(self, actor: User, comparison_id: uuid.UUID) -> Comparison:
        comparison = await self._session.scalar(
            select(Comparison)
            .where(Comparison.id == comparison_id, _all_visible(actor))
            .options(selectinload(Comparison.items))
        )
        if comparison is None:
            raise NotFoundError(NOT_FOUND)
        return comparison

    async def documents(self, comparisons: Sequence[Comparison]) -> dict[uuid.UUID, Document]:
        ids = {link.document_id for comparison in comparisons for link in comparison.documents}
        if not ids:
            return {}
        rows = await self._session.scalars(select(Document).where(Document.id.in_(ids)))
        return {row.id: row for row in rows}

    async def create(
        self,
        actor: User,
        inputs: Sequence[tuple[uuid.UUID, ComparisonRole]],
        meta: RequestMeta,
    ) -> Comparison:
        """Compare documents the user picked (e.g. an invoice with an order it does not cite)."""
        if len({document_id for document_id, _ in inputs}) != len(inputs):
            msg = "Each document may appear once."
            raise UnprocessableContentError(msg)
        facts: list[tuple[ComparisonRole, DocumentFacts]] = []
        subject_document: Document | None = None
        for document_id, role in inputs:
            document = await self._session.scalar(
                select(Document).where(Document.id == document_id, visible_documents(actor))
            )
            if document is None:
                msg = f"Document {document_id} not found."
                raise NotFoundError(msg)
            if document.document_type != _ROLE_TYPE[role]:
                msg = (
                    f"{document.display_filename} is not a {role.value.lower().replace('_', ' ')}."
                )
                raise UnprocessableContentError(msg)
            if document.status not in PROCESSED:
                msg = f"{document.display_filename} has not been processed yet."
                raise UnprocessableContentError(msg)
            found = await load_facts(self._session, document)
            if found is None:
                msg = f"{document.display_filename} has no extracted fields."
                raise UnprocessableContentError(msg)
            facts.append((role, found))
            if role in (ComparisonRole.INVOICE, ComparisonRole.DELIVERY_NOTE) and (
                subject_document is None or role == ComparisonRole.INVOICE
            ):
                subject_document = document
        invoices = [f for role, f in facts if role == ComparisonRole.INVOICE]
        orders = [f for role, f in facts if role == ComparisonRole.PURCHASE_ORDER]
        notes = [f for role, f in facts if role == ComparisonRole.DELIVERY_NOTE]
        if len(invoices) > 1 or len(orders) > 1:
            msg = "Compare at most one invoice and one purchase order at a time."
            raise UnprocessableContentError(msg)
        tolerances = tolerances_from_rules(
            await load_rules(self._session),
            min_confidence=self._settings.comparison_min_confidence,
        )
        if invoices:
            outcome = compare_invoice(invoices[0], orders[0] if orders else None, notes, tolerances)
        elif orders and len(notes) == 1:
            outcome = compare_delivery(notes[0], orders[0], tolerances)
        else:
            msg = (
                "Choose an invoice with a purchase order and/or delivery notes, or one delivery "
                "note with a purchase order."
            )
            raise UnprocessableContentError(msg)
        assert subject_document is not None  # noqa: S101 - an invoice or delivery note is present
        record = store_comparison(
            outcome,
            origin=ComparisonOrigin.MANUAL,
            department_id=subject_document.department_id,
            tolerances=tolerances,
            requested_by=actor,
        )
        self._session.add(record)
        await self._session.flush()
        record_audit_event(
            self._session,
            action=AuditAction.COMPARISON_CREATED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="comparison",
            entity_id=record.id,
            details={
                "comparison_type": record.comparison_type.value,
                "documents": [str(document_id) for document_id, _ in inputs],
                "summary": record.summary,
            },
        )
        await self._session.commit()
        return await self.get(actor, record.id)


def has_issues(comparison: Comparison) -> bool:
    return any(
        comparison.summary.get(status.value, 0)
        for status in (
            ComparisonItemStatus.MISMATCH,
            ComparisonItemStatus.MISSING,
            ComparisonItemStatus.UNCERTAIN,
        )
    )

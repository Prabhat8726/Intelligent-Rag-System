"""Database side of matching: facts from stored extractions, key facts, rules, locking."""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.db.models import (
    BusinessRule,
    Document,
    DocumentExtraction,
    DocumentPage,
    DocumentType,
)
from docintel.fields.store import resolved_from_row
from docintel.matching.facts import ClauseFact, DocumentFacts, facts_from_fields
from docintel.rules.engine import RuleDefinition, Severity
from docintel.versions.clauses import segment

_LOCK_NAMESPACE = 0x6D61  # "ma": matching


async def lock_department(session: AsyncSession, department_id: uuid.UUID | None) -> None:
    """Serialize matching within a department until the transaction ends.

    Every transaction that changes matching state (worker results, corrections, deletions,
    review decisions) takes this lock *before* any document row lock, so two documents of the
    same order processed at the same time see each other, and the lock order cannot deadlock.
    """
    digest = hashlib.blake2b(str(department_id).encode(), digest_size=4).digest()
    key = int.from_bytes(digest, "big", signed=True)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
        {"namespace": _LOCK_NAMESPACE, "key": key},
    )


async def current_extraction(
    session: AsyncSession, document_id: uuid.UUID
) -> DocumentExtraction | None:
    result: DocumentExtraction | None = await session.scalar(
        select(DocumentExtraction).where(
            DocumentExtraction.document_id == document_id,
            DocumentExtraction.is_current.is_(True),
        )
    )
    return result


async def load_facts(session: AsyncSession, document: Document) -> DocumentFacts | None:
    """Facts of the document's current extraction (None: nothing extracted)."""
    if document.document_type is None:
        return None
    record = await current_extraction(session, document.id)
    if record is None:
        return None
    facts = facts_from_fields(
        DocumentType(document.document_type),
        [resolved_from_row(row) for row in record.fields],
        field_ids={row.field_path: str(row.id) for row in record.fields},
        checks=record.checks or [],
        document_id=document.id,
        version_id=record.document_version_id,
        extraction_id=record.id,
        label=document.display_filename,
        created_at=document.created_at,
    )
    facts.review_level = record.review_level.value
    if facts.document_type == DocumentType.CONTRACT and record.document_version_id is not None:
        facts.clauses = await load_clauses(session, record.document_version_id)
    if document.vendor_id is not None:  # the document's vendor link is authoritative
        facts.vendor_id = str(document.vendor_id)
        facts.vendor_name = document.vendor.canonical_name if document.vendor else facts.vendor_name
    return facts


async def load_clauses(session: AsyncSession, version_id: uuid.UUID) -> list[ClauseFact]:
    """Numbered clauses of a processed version (its page texts, segmented)."""
    pages = list(
        await session.scalars(
            select(DocumentPage.text)
            .where(DocumentPage.document_version_id == version_id)
            .order_by(DocumentPage.page_number)
        )
    )
    return [
        ClauseFact(clause.number, clause.title, clause.text, clause.page)
        for clause in segment(pages)
        if clause.number is not None
    ]


def apply_key_facts(document: Document, facts: DocumentFacts | None) -> None:
    """Keep the indexed key facts on the document in step with its current extraction."""
    total = facts.total if facts else None
    document.number_key = (facts.number_key or None) if facts else None
    document.po_key = (facts.po_key or None) if facts else None
    document.document_date = facts.document_date if facts else None
    document.total_amount = total.quantize(Decimal("0.0001")) if total is not None else None
    currency = facts.currency if facts else None
    document.currency = currency if currency and len(currency) == 3 else None
    key = facts.vendor_key if facts else None
    document.vendor_key = key[:300] if key else None


def definition(row: BusinessRule) -> RuleDefinition:
    return RuleDefinition(
        code=row.code,
        rule_type=row.rule_type,
        name=row.name,
        description=row.description,
        applies_to=frozenset(DocumentType(value) for value in row.applies_to),
        severity=Severity(row.severity),
        params=dict(row.params or {}),
        enabled=row.is_enabled,
        version=row.version,
        rule_id=str(row.id),
    )


async def load_rules(session: AsyncSession) -> list[RuleDefinition]:
    rows = await session.scalars(select(BusinessRule).order_by(BusinessRule.code))
    return [definition(row) for row in rows]

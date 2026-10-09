"""Versions of a knowledge document and the dates on which each one is cited.

Every upload is its own `knowledge_documents` row; rows with the same `document_key` are the
versions of one document. At most one is ACTIVE (a partial unique index). When a version is
processed:

* no active version            -> it becomes ACTIVE;
* it is not older than the active one (by effective_from) -> it becomes ACTIVE, the previous
  one SUPERSEDED (a correction uploaded with the same date also replaces it);
* it is older                  -> it is stored as a historical (SUPERSEDED) version.

Retrieval as of a date D (default: today) uses each version's *retrieval window*, copied onto
its chunks so the filter runs inside the index scan:

* start = effective_from; a version without one that replaced another starts on the day it
  was published (processed), the first version without one has no start;
* end   = effective_to, capped at the day before the next version starts.

So a future-dated new version does not hide the current policy before its date, and asking
"as of 2025-06-30" cites the policy that applied then. Archiving the active version brings the
latest earlier version back.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.db.models import KnowledgeChunk, KnowledgeDocument, KnowledgeStatus

RETRIEVABLE_STATUSES = (KnowledgeStatus.ACTIVE, KnowledgeStatus.SUPERSEDED)


def is_newer(candidate_from: date | None, active_from: date | None) -> bool:
    """Whether an uploaded version replaces the active one (dates unknown: it does)."""
    if candidate_from is None or active_from is None:
        return True
    return candidate_from >= active_from


def window_start(document: KnowledgeDocument) -> date | None:
    if document.effective_from is not None:
        return document.effective_from
    if document.supersedes_id is not None and document.processed_at is not None:
        return document.processed_at.date()
    return None


def retrieval_window(
    document: KnowledgeDocument, successor: KnowledgeDocument | None
) -> tuple[date | None, date | None]:
    """(start, end) of the dates on which `document` is cited. An end before the start means
    never (a later version with an earlier or equal date replaced it entirely)."""
    start = window_start(document)
    end = document.effective_to
    successor_start = window_start(successor) if successor is not None else None
    if successor_start is not None:
        cap = successor_start - timedelta(days=1)
        end = cap if end is None else min(end, cap)
    return start, end


async def lock_document_key(session: AsyncSession, document_key: str) -> None:
    """Serialize version changes of one document (transaction-scoped advisory lock)."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"knowledge:{document_key}"},
    )


async def active_version(
    session: AsyncSession, document_key: str, *, exclude: object = None
) -> KnowledgeDocument | None:
    statement = (
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.document_key == document_key,
            KnowledgeDocument.status == KnowledgeStatus.ACTIVE,
            KnowledgeDocument.deleted_at.is_(None),
        )
        .with_for_update(of=KnowledgeDocument)
        .execution_options(populate_existing=True)
    )
    if exclude is not None:
        statement = statement.where(KnowledgeDocument.id != exclude)
    document: KnowledgeDocument | None = await session.scalar(statement)
    return document


async def latest_superseded(
    session: AsyncSession, document_key: str, *, exclude: object
) -> KnowledgeDocument | None:
    """The version to bring back when the active one is archived."""
    document: KnowledgeDocument | None = await session.scalar(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.document_key == document_key,
            KnowledgeDocument.status == KnowledgeStatus.SUPERSEDED,
            KnowledgeDocument.deleted_at.is_(None),
            KnowledgeDocument.id != exclude,
        )
        .order_by(
            KnowledgeDocument.effective_from.desc().nulls_last(),
            KnowledgeDocument.processed_at.desc().nulls_last(),
        )
        .limit(1)
        .with_for_update(of=KnowledgeDocument)
    )
    return document


async def mirror_to_chunks(
    session: AsyncSession, document: KnowledgeDocument, successor: KnowledgeDocument | None
) -> None:
    """Copy status and retrieval window onto the document's chunks."""
    start, end = retrieval_window(document, successor)
    await session.execute(
        update(KnowledgeChunk)
        .where(KnowledgeChunk.knowledge_document_id == document.id)
        .values(status=document.status, effective_from=start, effective_to=end)
    )

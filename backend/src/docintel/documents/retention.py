"""Retention: purge of deleted documents and storage reconciliation (Phase 11, ADR-075).

Deleting a document in the app is a soft delete (ADR-014): it disappears from every read, the
file and results stay for the audit trail. `purge_deleted_documents` removes documents deleted
more than RETENTION_DELETED_DAYS ago for good: the rows (everything about the document cascades:
versions, pages, extractions, findings, review tasks, workflows), then their files. Rows go
first, in one transaction per batch, so no row ever points at a missing file; a file whose
deletion fails is left unreferenced and the reconciliation removes it later. Each purge leaves an
audit event; the audit trail itself is append-only and is not purged.

`reconcile_storage` compares the stored objects with the keys the database refers to:
* orphans: stored, referenced by nothing (an upload interrupted between its two steps, a purge
  whose file deletion failed). Deleted on request, once older than a minimum age, so a file
  whose upload is still committing is never touched.
* missing: referenced, not stored. That is data loss; it is reported, never "repaired".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.audit.service import SYSTEM_REQUEST, AuditAction, record_audit_event
from docintel.core.logging import get_logger
from docintel.db.models import (
    ActorType,
    AuditOutcome,
    Document,
    DocumentPage,
    DocumentVersion,
    KnowledgeDocument,
    RefreshToken,
)
from docintel.storage import DocumentStorage
from docintel.storage.base import ListedObject, StorageError

logger = get_logger(__name__)

PURGE_BATCH_SIZE = 50
STORED_PREFIXES = ("documents/", "knowledge/")
# References younger than this when the listing starts may belong to a file still being
# written, or listed a moment before it was: they are not counted as missing.
REFERENCE_SETTLE_TIME = timedelta(minutes=5)
# Ended sessions keep their refresh tokens this long (reuse detection, then audit by log).
REFRESH_TOKEN_GRACE = timedelta(days=1)


@dataclass
class PurgeResult:
    dry_run: bool
    cutoff: datetime
    documents: int = 0
    files: int = 0
    files_failed: int = 0
    refresh_tokens: int = 0


async def _files_of(session: AsyncSession, document_ids: list[uuid.UUID]) -> list[str]:
    originals = await session.scalars(
        select(DocumentVersion.storage_key).where(DocumentVersion.document_id.in_(document_ids))
    )
    previews = await session.scalars(
        select(DocumentPage.preview_storage_key)
        .join(DocumentVersion, DocumentVersion.id == DocumentPage.document_version_id)
        .where(
            DocumentVersion.document_id.in_(document_ids),
            DocumentPage.preview_storage_key.is_not(None),
        )
    )
    return [*originals, *(key for key in previews if key)]


async def _delete_files(storage: DocumentStorage, keys: Iterable[str]) -> tuple[int, int]:
    deleted = failed = 0
    for key in keys:
        try:
            await storage.delete(key)
        except StorageError as exc:  # left unreferenced: the reconciliation finds it
            failed += 1
            logger.warning("retention.file_delete_failed", key=key, error=type(exc).__name__)
        else:
            deleted += 1
    return deleted, failed


async def purge_deleted_documents(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: DocumentStorage,
    *,
    older_than: timedelta,
    dry_run: bool = False,
    now: datetime | None = None,
    batch_size: int = PURGE_BATCH_SIZE,
) -> PurgeResult:
    now = now or datetime.now(UTC)
    result = PurgeResult(dry_run=dry_run, cutoff=now - older_than)
    due = Document.deleted_at.is_not(None) & (Document.deleted_at < result.cutoff)
    token_cutoff = now - REFRESH_TOKEN_GRACE
    ended = or_(
        RefreshToken.expires_at < token_cutoff, RefreshToken.session_expires_at < token_cutoff
    )

    if dry_run:
        async with sessionmaker() as session:
            ids = list(await session.scalars(select(Document.id).where(due)))
            result.documents = len(ids)
            result.files = len(await _files_of(session, ids)) if ids else 0
            result.refresh_tokens = (
                await session.scalar(select(func.count()).select_from(RefreshToken).where(ended))
                or 0
            )
        return result

    while True:
        async with sessionmaker() as session, session.begin():
            batch = list(
                await session.execute(
                    select(Document.id, Document.deleted_at)
                    .where(due)
                    .order_by(Document.deleted_at)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            if not batch:
                break
            ids = [document_id for document_id, _ in batch]
            keys = await _files_of(session, ids)
            await session.execute(delete(Document).where(Document.id.in_(ids)))
            for document_id, deleted_at in batch:
                record_audit_event(
                    session,
                    action=AuditAction.DOCUMENT_PURGED,
                    outcome=AuditOutcome.SUCCESS,
                    meta=SYSTEM_REQUEST,
                    actor_type=ActorType.SYSTEM,
                    entity_type="document",
                    entity_id=document_id,
                    details={
                        "deleted_at": deleted_at.isoformat() if deleted_at else None,
                        "retention_cutoff": result.cutoff.isoformat(),
                    },
                )
        deleted, failed = await _delete_files(storage, keys)
        result.documents += len(ids)
        result.files += deleted
        result.files_failed += failed
        logger.info("retention.documents_purged", documents=len(ids), files=deleted)

    async with sessionmaker() as session, session.begin():
        purged_tokens = await session.execute(delete(RefreshToken).where(ended))
        result.refresh_tokens = purged_tokens.rowcount or 0  # type: ignore[attr-defined]
    return result


@dataclass
class Reconciliation:
    listed: int = 0
    referenced: int = 0
    orphans: list[ListedObject] = field(default_factory=list)
    too_recent: int = 0  # unreferenced, but younger than the minimum age
    missing: list[str] = field(default_factory=list)
    deleted: int = 0
    delete_failed: int = 0


async def _referenced_keys(
    session: AsyncSession, backend: str, settled_before: datetime
) -> tuple[set[str], set[str]]:
    """(every key the database refers to, the keys of rows old enough to judge as missing)."""
    rows = [
        *(
            await session.execute(
                select(DocumentVersion.storage_key, DocumentVersion.created_at).where(
                    DocumentVersion.storage_backend == backend
                )
            )
        ),
        *(
            await session.execute(
                select(DocumentPage.preview_storage_key, DocumentPage.created_at)
                .join(DocumentVersion, DocumentVersion.id == DocumentPage.document_version_id)
                .where(
                    DocumentVersion.storage_backend == backend,
                    DocumentPage.preview_storage_key.is_not(None),
                )
            )
        ),
        *(
            await session.execute(
                select(KnowledgeDocument.storage_key, KnowledgeDocument.created_at).where(
                    KnowledgeDocument.storage_backend == backend
                )
            )
        ),
    ]
    referenced = {key for key, _ in rows if key}
    settled = {key for key, created_at in rows if key and created_at < settled_before}
    return referenced, settled


async def reconcile_storage(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: DocumentStorage,
    *,
    min_age: timedelta,
    delete_orphans: bool = False,
    now: datetime | None = None,
) -> Reconciliation:
    result = Reconciliation()
    started = now or datetime.now(UTC)
    # Listing first: a row committed after it can only make a key look missing, and those are
    # excluded by REFERENCE_SETTLE_TIME.
    stored: dict[str, ListedObject] = {}
    for prefix in STORED_PREFIXES:
        async for listed in storage.list_objects(prefix):
            stored[listed.key] = listed
    result.listed = len(stored)

    async with sessionmaker() as session:
        referenced, settled = await _referenced_keys(
            session, storage.backend.value, started - REFERENCE_SETTLE_TIME
        )
    result.referenced = len(referenced)
    result.missing = sorted(settled - stored.keys())

    for key in sorted(stored.keys() - referenced):
        listed = stored[key]
        if started - listed.modified_at < min_age:
            result.too_recent += 1
        else:
            result.orphans.append(listed)

    if delete_orphans and result.orphans:
        result.deleted, result.delete_failed = await _delete_files(
            storage, (orphan.key for orphan in result.orphans)
        )
        logger.info("retention.orphans_deleted", deleted=result.deleted)
    if result.missing:
        logger.error("retention.files_missing", count=len(result.missing))
    return result

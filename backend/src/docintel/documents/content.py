"""Read access to understanding results (pages, previews, tables, classifications) and human
classification corrections. Callers resolve the document through DocumentService.get first, so
the access policy is applied before anything here runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.core.config import Settings
from docintel.core.errors import NotFoundError
from docintel.core.logging import get_logger
from docintel.db.models import (
    AuditOutcome,
    ClassificationMethod,
    Document,
    DocumentClassification,
    DocumentExtraction,
    DocumentPage,
    DocumentStatus,
    DocumentTable,
    DocumentType,
    JobType,
    ReviewReason,
    User,
)
from docintel.fields.schemas import schema_for
from docintel.matching.service import MatchingService
from docintel.matching.store import lock_department
from docintel.review.items import RULE_REASONS
from docintel.storage import DocumentStorage, StorageError
from docintel.workers.queue import enqueue_job

logger = get_logger(__name__)

EXTRACTION_REASONS = frozenset(
    {
        ReviewReason.EXTRACTION_FAILED.value,
        ReviewReason.MISSING_REQUIRED_FIELDS.value,
        ReviewReason.EXTRACTION_UNCERTAIN.value,
        ReviewReason.EXTRACTION_INCONSISTENT.value,
    }
)

PAGE_NOT_FOUND = "Page not found."
HISTORY_LIMIT = 20


class DocumentContentService:
    def __init__(self, session: AsyncSession, storage: DocumentStorage) -> None:
        self._session = session
        self._storage = storage

    async def pages(self, document: Document) -> list[DocumentPage]:
        if document.current_version_id is None:
            return []
        result = await self._session.scalars(
            select(DocumentPage)
            .where(DocumentPage.document_version_id == document.current_version_id)
            .order_by(DocumentPage.page_number)
        )
        return list(result)

    async def page(self, document: Document, page_number: int) -> DocumentPage:
        page = await self._session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_version_id == document.current_version_id,
                DocumentPage.page_number == page_number,
            )
        )
        if page is None:
            raise NotFoundError(PAGE_NOT_FOUND)
        return page

    async def open_page_image(self, document: Document, page_number: int) -> AsyncIterator[bytes]:
        page = await self.page(document, page_number)
        if page.preview_storage_key is None:
            raise NotFoundError(PAGE_NOT_FOUND)
        chunks = self._storage.open_stream(page.preview_storage_key)
        try:
            first = await anext(chunks)
        except StopAsyncIteration:
            first = b""
        except StorageError as exc:
            logger.error("document.page_image_unavailable", document_id=str(document.id))
            raise NotFoundError(PAGE_NOT_FOUND) from exc

        async def stream() -> AsyncIterator[bytes]:
            if first:
                yield first
            async for chunk in chunks:
                yield chunk

        return stream()

    async def tables(self, document: Document) -> list[DocumentTable]:
        if document.current_version_id is None:
            return []
        result = await self._session.scalars(
            select(DocumentTable)
            .where(DocumentTable.document_version_id == document.current_version_id)
            .order_by(DocumentTable.table_index)
        )
        return list(result)

    async def classifications(self, document: Document) -> list[DocumentClassification]:
        """Newest first; the current one (if any) is included."""
        result = await self._session.scalars(
            select(DocumentClassification)
            .where(DocumentClassification.document_id == document.id)
            .order_by(DocumentClassification.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
        return list(result)

    async def correct_classification(
        self,
        actor: User,
        document: Document,
        label: DocumentType,
        note: str | None,
        meta: RequestMeta,
        *,
        settings: Settings,
    ) -> DocumentClassification:
        """Record a human label as the current classification (worker results never override it).

        If the label changes the extraction schema, the version is queued for re-extraction with
        the new schema; a label without a schema (OTHER) retires the current extraction.
        """
        if document.current_version_id is None:
            raise NotFoundError("Document not found.")
        # Same lock order as the worker's result transaction: department, then the document.
        await lock_department(self._session, document.department_id)
        locked = await self._session.scalar(
            select(Document)
            .where(Document.id == document.id)
            .with_for_update(of=Document)
            .execution_options(populate_existing=True)
        )
        if locked is None:
            raise NotFoundError("Document not found.")
        current = await self._session.scalar(
            select(DocumentClassification).where(
                DocumentClassification.document_id == locked.id,
                DocumentClassification.is_current.is_(True),
            )
        )
        previous = None
        if current is not None:
            previous = {
                "label": current.label.value,
                "method": current.method.value,
                "confidence": str(current.confidence),
            }
            current.is_current = False
            await self._session.flush()  # free the "one current row" index before inserting
        record = DocumentClassification(
            document_id=locked.id,
            document_version_id=locked.current_version_id,
            label=label,
            confidence=Decimal(1),
            method=ClassificationMethod.HUMAN,
            signals={"previous": previous},
            note=note,
            created_by_id=actor.id,
            is_current=True,
        )
        self._session.add(record)
        locked.document_type = label
        locked.type_confidence = Decimal(1)
        locked.review_reasons = [
            reason
            for reason in locked.review_reasons
            if reason != ReviewReason.CLASSIFICATION_UNCERTAIN.value
        ]
        reextract = await self._follow_schema(actor, locked, label, settings.job_max_attempts)
        if reextract is None and locked.status in (
            DocumentStatus.REVIEW_REQUIRED,
            DocumentStatus.COMPLETED,
        ):
            # Same extraction (or none any more): re-run matching and settle the review task.
            await MatchingService(self._session, settings).refresh(locked, actor=actor)
        record_audit_event(
            self._session,
            action=AuditAction.DOCUMENT_CLASSIFICATION_CORRECTED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="document",
            entity_id=locked.id,
            details={
                "from": previous,
                "to": label.value,
                "note": bool(note),
                "reextraction_job_id": str(reextract) if reextract else None,
            },
        )
        await self._session.commit()
        await self._session.refresh(record)
        return record

    async def _follow_schema(
        self, actor: User, document: Document, label: DocumentType, max_attempts: int
    ) -> uuid.UUID | None:
        """Keep the extraction in line with the human label. Returns a queued job id, if any."""
        current = await self._session.scalar(
            select(DocumentExtraction).where(
                DocumentExtraction.document_id == document.id,
                DocumentExtraction.is_current.is_(True),
            )
        )
        schema = schema_for(label)
        if current is not None and schema is not None and current.schema_name == schema.name:
            return None
        # Extraction findings and the rules evaluated on them belong to the old schema.
        document.review_reasons = [
            reason
            for reason in document.review_reasons
            if reason not in EXTRACTION_REASONS and reason not in RULE_REASONS
        ]
        if schema is None:
            if current is not None:
                current.is_current = False
            document.vendor_id = None
            return None
        if document.current_version_id is None or document.status in (
            DocumentStatus.PENDING,
            DocumentStatus.PROCESSING,
        ):
            return None  # the pending run will use the human label
        try:
            async with self._session.begin_nested():
                job = await enqueue_job(
                    self._session,
                    job_type=JobType.DOCUMENT_PROCESSING,
                    max_attempts=max_attempts,
                    document_id=document.id,
                    document_version_id=document.current_version_id,
                    requested_by_id=actor.id,
                    payload={"reason": "classification corrected"},
                )
        except IntegrityError:
            return None  # already queued; that run will use the human label
        document.status = DocumentStatus.PENDING
        return job.id

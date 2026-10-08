"""Read access to understanding results (pages, previews, tables, classifications) and human
classification corrections. Callers resolve the document through DocumentService.get first, so
the access policy is applied before anything here runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.core.errors import NotFoundError
from docintel.core.logging import get_logger
from docintel.db.models import (
    AuditOutcome,
    ClassificationMethod,
    Document,
    DocumentClassification,
    DocumentPage,
    DocumentStatus,
    DocumentTable,
    DocumentType,
    ReviewReason,
    User,
)
from docintel.storage import DocumentStorage, StorageError

logger = get_logger(__name__)

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
    ) -> DocumentClassification:
        """Record a human label as the current classification (worker results never override it)."""
        if document.current_version_id is None:
            raise NotFoundError("Document not found.")
        # Serialize with the worker's result transaction, which locks the same row.
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
        if locked.status == DocumentStatus.REVIEW_REQUIRED and not locked.review_reasons:
            locked.status = DocumentStatus.COMPLETED
        record_audit_event(
            self._session,
            action=AuditAction.DOCUMENT_CLASSIFICATION_CORRECTED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="document",
            entity_id=locked.id,
            details={"from": previous, "to": label.value, "note": bool(note)},
        )
        await self._session.commit()
        await self._session.refresh(record)
        return record

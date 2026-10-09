"""Document ingestion use-cases (Module 1).

Upload sequence (docs/architecture/03-data-flow.md, flow A):
  spool to a private temp file (size-capped) -> validate -> exact-duplicate lookup (scoped)
  -> write blob under a server-generated key -> ONE transaction: document + version + job +
  audit -> on commit failure, delete the blob (compensation).
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import Select, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.policies import visible_documents
from docintel.core.config import Settings
from docintel.core.errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    UnprocessableContentError,
)
from docintel.core.logging import get_logger
from docintel.db.models import (
    AuditOutcome,
    Department,
    Document,
    DocumentChunk,
    DocumentSource,
    DocumentStatus,
    DocumentType,
    DocumentVersion,
    JobType,
    ProcessingJob,
    Role,
    Sensitivity,
    User,
)
from docintel.documents.validation import UploadLimits, ValidatedUpload, validate_upload
from docintel.matching.service import MatchingService
from docintel.matching.store import lock_department
from docintel.review.service import cancel_open_task
from docintel.storage import DocumentStorage, StorageError, document_object_key
from docintel.workers.queue import cancel_queued_jobs, enqueue_job

logger = get_logger(__name__)

_SPOOL_CHUNK = 1024 * 1024
NOT_FOUND = "Document not found."


@dataclass(frozen=True, slots=True)
class DocumentFilters:
    status: DocumentStatus | None = None
    document_type: DocumentType | None = None
    owned_by_me: bool = False
    filename_contains: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    vendor_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class DocumentDownload:
    version: DocumentVersion
    filename: str
    chunks: AsyncIterator[bytes]


async def spool_upload(upload: UploadFile, destination: Path, *, max_bytes: int) -> None:
    """Copy the multipart part to a private file, enforcing the size cap while copying."""
    written = 0
    with destination.open("wb") as handle:
        while chunk := await upload.read(_SPOOL_CHUNK):
            written += len(chunk)
            if written > max_bytes:
                limit_mb = max_bytes // (1024 * 1024)
                msg = f"The file exceeds the maximum upload size of {limit_mb} MB."
                raise PayloadTooLargeError(msg)
            handle.write(chunk)


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class DocumentService:
    def __init__(self, session: AsyncSession, storage: DocumentStorage, settings: Settings) -> None:
        self._session = session
        self._storage = storage
        self._settings = settings

    @property
    def _limits(self) -> UploadLimits:
        return UploadLimits(
            max_bytes=self._settings.upload_max_bytes,
            max_pages=self._settings.upload_max_pages,
            max_image_pixels=self._settings.upload_max_image_pixels,
        )

    # ------------------------------------------------------------------------ upload
    async def _spool(self, upload: UploadFile, destination: Path) -> None:
        await spool_upload(upload, destination, max_bytes=self._limits.max_bytes)

    async def _resolve_department(
        self, actor: User, requested: uuid.UUID | None
    ) -> uuid.UUID | None:
        if requested is None:
            return actor.department_id
        if actor.role != Role.ADMIN:
            msg = "Only administrators can file documents into another department."
            raise PermissionDeniedError(msg)
        if await self._session.get(Department, requested) is None:
            msg = "The requested department does not exist."
            raise UnprocessableContentError(msg)
        return requested

    async def _find_exact_duplicate(
        self, actor: User, sha256: str, exclude: uuid.UUID | None = None
    ) -> uuid.UUID | None:
        statement = (
            select(Document.id)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(DocumentVersion.sha256 == sha256, visible_documents(actor))
            .order_by(Document.created_at)
            .limit(1)
        )
        if exclude is not None:
            statement = statement.where(Document.id != exclude)
        found: uuid.UUID | None = await self._session.scalar(statement)
        return found

    async def upload(
        self,
        *,
        actor: User,
        upload: UploadFile,
        sensitivity: Sensitivity,
        department_id: uuid.UUID | None,
        meta: RequestMeta,
        source: DocumentSource = DocumentSource.UPLOAD,
    ) -> Document:
        target_department = await self._resolve_department(actor, department_id)
        workdir = Path(tempfile.mkdtemp(prefix="docintel-upload-"))
        try:
            spooled = workdir / "upload.bin"
            await self._spool(upload, spooled)
            validated = await validate_upload(
                spooled,
                filename=upload.filename,
                content_type=upload.content_type,
                limits=self._limits,
            )
            return await self._store(
                actor=actor,
                validated=validated,
                source_path=spooled,
                sensitivity=sensitivity,
                department_id=target_department,
                source=source,
                meta=meta,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _store(
        self,
        *,
        actor: User,
        validated: ValidatedUpload,
        source_path: Path,
        sensitivity: Sensitivity,
        department_id: uuid.UUID | None,
        source: DocumentSource,
        meta: RequestMeta,
    ) -> Document:
        duplicate_of = await self._find_exact_duplicate(actor, validated.sha256)
        document_id, version_id = uuid.uuid4(), uuid.uuid4()
        key = document_object_key(document_id, 1, validated.storage_extension)

        await self._storage.put_file(key, source_path, content_type=validated.mime_type)
        try:
            document = Document(
                id=document_id,
                display_filename=validated.display_filename,
                status=DocumentStatus.PENDING,
                sensitivity=sensitivity,
                source=source,
                owner_id=actor.id,
                department_id=department_id,
                duplicate_of_id=duplicate_of,
                duplicate_reason="EXACT_FILE_HASH" if duplicate_of else None,
            )
            version = DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_number=1,
                storage_backend=self._storage.backend,
                storage_key=key,
                original_filename=validated.display_filename,
                file_kind=validated.kind.value,
                mime_type=validated.mime_type,
                size_bytes=validated.size_bytes,
                sha256=validated.sha256,
                page_count=validated.page_count,
                uploaded_by_id=actor.id,
            )
            document.current_version = version
            self._session.add_all([document, version])
            await self._session.flush()
            job = await enqueue_job(
                self._session,
                job_type=JobType.DOCUMENT_PROCESSING,
                max_attempts=self._settings.job_max_attempts,
                document_id=document_id,
                document_version_id=version_id,
                requested_by_id=actor.id,
            )
            record_audit_event(
                self._session,
                action=AuditAction.DOCUMENT_UPLOADED,
                outcome=AuditOutcome.SUCCESS,
                meta=meta,
                actor=actor,
                entity_type="document",
                entity_id=document_id,
                details={
                    "version_id": str(version_id),
                    "job_id": str(job.id),
                    "sha256": validated.sha256,
                    "size_bytes": validated.size_bytes,
                    "mime_type": validated.mime_type,
                    "page_count": validated.page_count,
                    "sensitivity": sensitivity.value,
                    "duplicate_of": str(duplicate_of) if duplicate_of else None,
                },
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            try:
                await self._storage.delete(key)
            except StorageError:
                logger.exception("document.upload.orphaned_blob", storage_key=key)
            raise
        logger.info(
            "document.uploaded",
            document_id=str(document_id),
            size_bytes=validated.size_bytes,
            pages=validated.page_count,
            duplicate=duplicate_of is not None,
        )
        return await self.get(actor, document_id)

    async def upload_version(
        self, *, actor: User, document_id: uuid.UUID, upload: UploadFile, meta: RequestMeta
    ) -> Document:
        """Add a new file version (e.g. a revised contract); it becomes current and is processed.

        Earlier versions keep their pages and stay comparable (GET .../versions/compare).
        """
        document = await self.get(actor, document_id)
        if document.status in (DocumentStatus.PENDING, DocumentStatus.PROCESSING):
            msg = "The document is being processed; add the new version when it has finished."
            raise ConflictError(msg)
        workdir = Path(tempfile.mkdtemp(prefix="docintel-upload-"))
        try:
            spooled = workdir / "upload.bin"
            await self._spool(upload, spooled)
            validated = await validate_upload(
                spooled,
                filename=upload.filename,
                content_type=upload.content_type,
                limits=self._limits,
            )
            current = document.current_version
            if current is not None and current.sha256 == validated.sha256:
                msg = "The file is identical to the current version."
                raise ConflictError(msg)
            return await self._store_version(actor, document, validated, spooled, meta)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _store_version(
        self,
        actor: User,
        document: Document,
        validated: ValidatedUpload,
        source_path: Path,
        meta: RequestMeta,
    ) -> Document:
        latest = await self._session.scalar(
            select(func.max(DocumentVersion.version_number)).where(
                DocumentVersion.document_id == document.id
            )
        )
        number = (latest or 0) + 1
        version_id = uuid.uuid4()
        key = document_object_key(document.id, number, validated.storage_extension)
        await self._storage.put_file(key, source_path, content_type=validated.mime_type)
        try:
            locked = await self._session.scalar(
                select(Document)
                .where(Document.id == document.id)
                .with_for_update(of=Document)
                .execution_options(populate_existing=True)
            )
            if locked is None or locked.deleted_at is not None:
                raise NotFoundError(NOT_FOUND)
            if locked.status in (DocumentStatus.PENDING, DocumentStatus.PROCESSING):
                msg = "The document is being processed; add the new version when it has finished."
                raise ConflictError(msg)
            version = DocumentVersion(
                id=version_id,
                document_id=locked.id,
                version_number=number,
                storage_backend=self._storage.backend,
                storage_key=key,
                original_filename=validated.display_filename,
                file_kind=validated.kind.value,
                mime_type=validated.mime_type,
                size_bytes=validated.size_bytes,
                sha256=validated.sha256,
                page_count=validated.page_count,
                uploaded_by_id=actor.id,
            )
            self._session.add(version)
            await self._session.flush()
            locked.current_version_id = version_id
            locked.status = DocumentStatus.PENDING
            locked.processing_error = None
            duplicate_of = await self._find_exact_duplicate(actor, validated.sha256, locked.id)
            if duplicate_of is not None:
                locked.duplicate_of_id, locked.duplicate_reason = duplicate_of, "EXACT_FILE_HASH"
            elif locked.duplicate_reason == "EXACT_FILE_HASH":
                locked.duplicate_of_id, locked.duplicate_reason = None, None
            job = await enqueue_job(
                self._session,
                job_type=JobType.DOCUMENT_PROCESSING,
                max_attempts=self._settings.job_max_attempts,
                document_id=locked.id,
                document_version_id=version_id,
                requested_by_id=actor.id,
                payload={"reason": "new version"},
            )
            record_audit_event(
                self._session,
                action=AuditAction.DOCUMENT_VERSION_UPLOADED,
                outcome=AuditOutcome.SUCCESS,
                meta=meta,
                actor=actor,
                entity_type="document",
                entity_id=locked.id,
                details={
                    "version_id": str(version_id),
                    "version_number": number,
                    "job_id": str(job.id),
                    "sha256": validated.sha256,
                    "size_bytes": validated.size_bytes,
                    "page_count": validated.page_count,
                },
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            try:
                await self._storage.delete(key)
            except StorageError:
                logger.exception("document.version.orphaned_blob", storage_key=key)
            raise
        return await self.get(actor, document.id)

    # ------------------------------------------------------------------------ reads
    def _visible(self, actor: User) -> Select[Document]:
        return select(Document).where(visible_documents(actor))

    async def get(self, actor: User, document_id: uuid.UUID) -> Document:
        document = await self._session.scalar(
            self._visible(actor)
            .where(Document.id == document_id)
            # Reload server-maintained columns (e.g. updated_at) expired by earlier writes.
            .execution_options(populate_existing=True)
        )
        if document is None:
            raise NotFoundError(NOT_FOUND)
        return document

    async def list(
        self, actor: User, filters: DocumentFilters, *, limit: int, offset: int
    ) -> tuple[list[Document], int]:
        statement = self._visible(actor)
        if filters.status is not None:
            statement = statement.where(Document.status == filters.status)
        if filters.document_type is not None:
            statement = statement.where(Document.document_type == filters.document_type)
        if filters.owned_by_me:
            statement = statement.where(Document.owner_id == actor.id)
        if filters.filename_contains:
            pattern = f"%{escape_like(filters.filename_contains)}%"
            statement = statement.where(Document.display_filename.ilike(pattern, escape="\\"))
        if filters.created_from is not None:
            statement = statement.where(Document.created_at >= filters.created_from)
        if filters.created_to is not None:
            statement = statement.where(Document.created_at < filters.created_to)
        if filters.vendor_id is not None:
            statement = statement.where(Document.vendor_id == filters.vendor_id)

        total = await self._session.scalar(
            select(func.count()).select_from(statement.order_by(None).subquery())
        )
        page = await self._session.scalars(
            statement.order_by(Document.created_at.desc(), Document.id).limit(limit).offset(offset)
        )
        return list(page.unique()), int(total or 0)

    async def latest_job(self, document_id: uuid.UUID) -> ProcessingJob | None:
        job: ProcessingJob | None = await self._session.scalar(
            select(ProcessingJob)
            .where(ProcessingJob.document_id == document_id)
            .order_by(ProcessingJob.created_at.desc())
            .limit(1)
        )
        return job

    async def open_download(
        self, actor: User, document_id: uuid.UUID, meta: RequestMeta
    ) -> DocumentDownload:
        document = await self.get(actor, document_id)
        version = document.current_version
        if version is None:
            raise NotFoundError(NOT_FOUND)
        chunks = self._storage.open_stream(version.storage_key)
        try:
            # Fetch the first chunk now so a missing blob fails before response headers are sent.
            first = await anext(chunks)
        except StopAsyncIteration:
            first = b""
        except StorageError as exc:
            logger.error("document.download.blob_unavailable", document_id=str(document_id))
            raise NotFoundError(NOT_FOUND) from exc
        record_audit_event(
            self._session,
            action=AuditAction.DOCUMENT_DOWNLOADED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="document",
            entity_id=document_id,
            details={"version_id": str(version.id)},
        )
        await self._session.commit()

        async def _stream() -> AsyncIterator[bytes]:
            if first:
                yield first
            async for chunk in chunks:
                yield chunk

        return DocumentDownload(
            version=version, filename=version.original_filename, chunks=_stream()
        )

    # ------------------------------------------------------------------------ writes
    async def soft_delete(self, actor: User, document_id: uuid.UUID, meta: RequestMeta) -> None:
        document = await self.get(actor, document_id)
        await lock_department(self._session, document.department_id)
        document.deleted_at = datetime.now(UTC)
        document.deleted_by_id = actor.id
        cancelled = await cancel_queued_jobs(self._session, document_id=document_id)
        await cancel_open_task(self._session, document, "The document was deleted.")
        # Its text leaves the search index (the stored file and results stay for the audit trail).
        await self._session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )
        # Documents compared with it, or flagged as its duplicates, are re-evaluated without it.
        await MatchingService(self._session, self._settings).refresh(document, actor=actor)
        record_audit_event(
            self._session,
            action=AuditAction.DOCUMENT_DELETED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="document",
            entity_id=document_id,
            details={"cancelled_jobs": cancelled, "mode": "soft"},
        )
        await self._session.commit()

    async def request_reprocess(
        self, actor: User, document_id: uuid.UUID, meta: RequestMeta
    ) -> ProcessingJob:
        document = await self.get(actor, document_id)
        if document.current_version_id is None:
            raise NotFoundError(NOT_FOUND)
        try:
            async with self._session.begin_nested():
                job = await enqueue_job(
                    self._session,
                    job_type=JobType.DOCUMENT_PROCESSING,
                    max_attempts=self._settings.job_max_attempts,
                    document_id=document.id,
                    document_version_id=document.current_version_id,
                    requested_by_id=actor.id,
                )
        except IntegrityError as exc:
            msg = "The document is already queued or being processed."
            raise ConflictError(msg) from exc
        document.status = DocumentStatus.PENDING
        document.processing_error = None
        record_audit_event(
            self._session,
            action=AuditAction.DOCUMENT_REPROCESS_REQUESTED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="document",
            entity_id=document.id,
            details={"job_id": str(job.id)},
        )
        await self._session.commit()
        return job

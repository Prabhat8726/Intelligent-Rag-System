"""Knowledge base use-cases (Module 12): upload, list, read, archive.

Upload: spool (size-capped) -> validate (text or PDF/image) -> metadata (form, then front
matter) -> scope and version checks -> blob under a server-generated key -> ONE transaction:
knowledge document + job + audit -> on failure, delete the blob (compensation).

Scope: a knowledge document is organization-wide (no department) or restricted to one
department. Managers publish organization-wide or for their own department; only
administrators file into another department. Every version of a document (same document_key)
keeps the scope of the first one, so a new version can never widen or move its audience.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.policies import visible_knowledge
from docintel.core.config import Settings
from docintel.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableContentError,
)
from docintel.core.logging import get_logger
from docintel.db.models import (
    AuditOutcome,
    Department,
    JobType,
    KnowledgeCategory,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeStatus,
    Role,
    User,
)
from docintel.documents.service import escape_like, spool_upload
from docintel.documents.validation import UploadLimits
from docintel.knowledge.lifecycle import (
    latest_superseded,
    lock_document_key,
    mirror_to_chunks,
)
from docintel.knowledge.validation import (
    KnowledgeMetadata,
    MetadataInput,
    ValidatedKnowledgeFile,
    front_matter,
    resolve_metadata,
    validate_knowledge_file,
)
from docintel.storage import DocumentStorage, StorageError, knowledge_object_key
from docintel.workers.queue import cancel_queued_jobs, enqueue_job

logger = get_logger(__name__)

NOT_FOUND = "Knowledge document not found."


@dataclass(frozen=True, slots=True)
class KnowledgeFilters:
    status: KnowledgeStatus | None = None
    category: KnowledgeCategory | None = None
    document_key: str | None = None
    title_contains: str | None = None


class KnowledgeService:
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
    async def upload(
        self,
        *,
        actor: User,
        upload: UploadFile,
        form: MetadataInput,
        department_id: uuid.UUID | None,
        meta: RequestMeta,
    ) -> KnowledgeDocument:
        workdir = Path(tempfile.mkdtemp(prefix="docintel-knowledge-upload-"))
        try:
            spooled = workdir / "upload.bin"
            await spool_upload(upload, spooled, max_bytes=self._limits.max_bytes)
            validated = await asyncio.to_thread(
                validate_knowledge_file,
                spooled,
                filename=upload.filename,
                content_type=upload.content_type,
                limits=self._limits,
                max_text_bytes=self._settings.knowledge_text_max_bytes,
            )
            front, heading = front_matter(validated)
            metadata = resolve_metadata(
                form, front, first_heading=heading, filename=validated.display_filename
            )
            department = await self._resolve_department(actor, department_id, metadata)
            await self._check_versions(metadata, department, validated)
            return await self._store(actor, validated, metadata, department, spooled, meta)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _resolve_department(
        self, actor: User, requested: uuid.UUID | None, metadata: KnowledgeMetadata
    ) -> uuid.UUID | None:
        department_id = requested
        if department_id is None and metadata.department_name is not None:
            department_id = await self._session.scalar(
                select(Department.id).where(
                    func.lower(Department.name) == metadata.department_name.lower()
                )
            )
            if department_id is None:
                msg = f"Unknown department in front matter: {metadata.department_name!r}."
                raise UnprocessableContentError(msg)
        if department_id is None:
            return None  # organization-wide
        if actor.role != Role.ADMIN and department_id != actor.department_id:
            msg = "Only administrators can publish knowledge for another department."
            raise PermissionDeniedError(msg)
        if await self._session.get(Department, department_id) is None:
            msg = "The requested department does not exist."
            raise UnprocessableContentError(msg)
        return department_id

    async def _check_versions(
        self,
        metadata: KnowledgeMetadata,
        department_id: uuid.UUID | None,
        validated: ValidatedKnowledgeFile,
    ) -> None:
        versions = (
            await self._session.scalars(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.document_key == metadata.document_key,
                    KnowledgeDocument.deleted_at.is_(None),
                )
            )
        ).all()
        for version in versions:
            if version.department_id != department_id:
                msg = (
                    "This document_key belongs to a document with a different audience; "
                    "every version must keep the scope of the first one."
                )
                raise ConflictError(msg)
            if version.status == KnowledgeStatus.PROCESSING:
                msg = "A version of this document is being processed; try again when it is done."
                raise ConflictError(msg)
            if version.sha256 == validated.sha256 and version.status != KnowledgeStatus.FAILED:
                msg = f"This file is already in the knowledge base ({version.title})."
                raise ConflictError(msg)

    async def _store(
        self,
        actor: User,
        validated: ValidatedKnowledgeFile,
        metadata: KnowledgeMetadata,
        department_id: uuid.UUID | None,
        source_path: Path,
        meta: RequestMeta,
    ) -> KnowledgeDocument:
        document_id = uuid.uuid4()
        key = knowledge_object_key(document_id, validated.storage_extension)
        await self._storage.put_file(key, source_path, content_type=validated.mime_type)
        try:
            document = KnowledgeDocument(
                id=document_id,
                document_key=metadata.document_key,
                title=metadata.title,
                category=metadata.category,
                version_label=metadata.version_label,
                department_id=department_id,
                sensitivity=metadata.sensitivity,
                effective_from=metadata.effective_from,
                effective_to=metadata.effective_to,
                status=KnowledgeStatus.PROCESSING,
                source_format=validated.format,
                original_filename=validated.display_filename,
                mime_type=validated.mime_type,
                size_bytes=validated.size_bytes,
                sha256=validated.sha256,
                storage_backend=self._storage.backend,
                storage_key=key,
                page_count=validated.page_count,
                uploaded_by_id=actor.id,
            )
            self._session.add(document)
            await self._session.flush()
            job = await enqueue_job(
                self._session,
                job_type=JobType.KNOWLEDGE_PROCESSING,
                max_attempts=self._settings.job_max_attempts,
                knowledge_document_id=document_id,
                requested_by_id=actor.id,
                payload={"title_from_content": metadata.title_from_content},
            )
            record_audit_event(
                self._session,
                action=AuditAction.KNOWLEDGE_UPLOADED,
                outcome=AuditOutcome.SUCCESS,
                meta=meta,
                actor=actor,
                entity_type="knowledge_document",
                entity_id=document_id,
                details={
                    "job_id": str(job.id),
                    "document_key": metadata.document_key,
                    "version": metadata.version_label,
                    "category": metadata.category.value,
                    "sensitivity": metadata.sensitivity.value,
                    "department_id": str(department_id) if department_id else None,
                    "format": validated.format.value,
                    "sha256": validated.sha256,
                    "size_bytes": validated.size_bytes,
                },
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            try:
                await self._storage.delete(key)
            except StorageError:
                logger.exception("knowledge.upload.orphaned_blob", storage_key=key)
            raise
        logger.info(
            "knowledge.uploaded",
            knowledge_document_id=str(document_id),
            document_key=metadata.document_key,
            size_bytes=validated.size_bytes,
        )
        return await self.get(actor, document_id)

    # ------------------------------------------------------------------------ reads
    async def get(self, actor: User, document_id: uuid.UUID) -> KnowledgeDocument:
        document: KnowledgeDocument | None = await self._session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == document_id, visible_knowledge(actor))
            .execution_options(populate_existing=True)
        )
        if document is None:
            raise NotFoundError(NOT_FOUND)
        return document

    async def list(
        self, actor: User, filters: KnowledgeFilters, *, limit: int, offset: int
    ) -> tuple[list[KnowledgeDocument], int]:
        statement = select(KnowledgeDocument).where(visible_knowledge(actor))
        if filters.status is not None:
            statement = statement.where(KnowledgeDocument.status == filters.status)
        if filters.category is not None:
            statement = statement.where(KnowledgeDocument.category == filters.category)
        if filters.document_key:
            statement = statement.where(KnowledgeDocument.document_key == filters.document_key)
        if filters.title_contains:
            pattern = f"%{escape_like(filters.title_contains)}%"
            statement = statement.where(KnowledgeDocument.title.ilike(pattern, escape="\\"))
        total = await self._session.scalar(
            select(func.count()).select_from(statement.order_by(None).subquery())
        )
        page = await self._session.scalars(
            statement.order_by(
                KnowledgeDocument.document_key,  # versions of a document together, newest first
                KnowledgeDocument.effective_from.desc().nulls_last(),
                KnowledgeDocument.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return list(page), int(total or 0)

    async def chunks(self, actor: User, document_id: uuid.UUID) -> Sequence[KnowledgeChunk]:
        document = await self.get(actor, document_id)
        return (
            await self._session.scalars(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.knowledge_document_id == document.id)
                .order_by(KnowledgeChunk.chunk_index)
            )
        ).all()

    # ------------------------------------------------------------------------ writes
    async def archive(self, actor: User, document_id: uuid.UUID, meta: RequestMeta) -> None:
        """Remove a version from the knowledge base (soft delete: the row and file stay for
        the audit trail, its chunks are deleted). Archiving the active version brings back
        the latest earlier one."""
        document = await self.get(actor, document_id)  # managers: their own scope only
        await lock_document_key(self._session, document.document_key)
        locked: KnowledgeDocument | None = await self._session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == document_id, KnowledgeDocument.deleted_at.is_(None))
            .with_for_update(of=KnowledgeDocument)
            .execution_options(populate_existing=True)
        )
        if locked is None:
            raise NotFoundError(NOT_FOUND)
        was_active = locked.status == KnowledgeStatus.ACTIVE
        cancelled = await cancel_queued_jobs(self._session, knowledge_document_id=locked.id)
        locked.status = KnowledgeStatus.ARCHIVED
        locked.deleted_at = datetime.now(UTC)
        locked.chunk_count = 0
        await self._session.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.knowledge_document_id == locked.id)
        )
        await self._session.flush()
        restored: KnowledgeDocument | None = None
        if was_active:
            restored = await latest_superseded(
                self._session, locked.document_key, exclude=locked.id
            )
            if restored is not None:
                restored.status = KnowledgeStatus.ACTIVE
                await self._session.flush()
                await mirror_to_chunks(self._session, restored, None)
        record_audit_event(
            self._session,
            action=AuditAction.KNOWLEDGE_ARCHIVED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="knowledge_document",
            entity_id=locked.id,
            details={
                "document_key": locked.document_key,
                "was_active": was_active,
                "restored": str(restored.id) if restored else None,
                "cancelled_jobs": cancelled,
            },
        )
        await self._session.commit()

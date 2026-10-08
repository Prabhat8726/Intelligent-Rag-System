"""Document processing pipeline (worker side).

Stages do file work only; database writes happen in short transactions owned by the handler:
  prepare  -> document PROCESSING (or skip if deleted)
  stages   -> integrity (re-hash the stored blob), inspect (per-page native vs OCR)
  success  -> results + document COMPLETED + job COMPLETED + audit, in ONE transaction
  failure  -> job retried or FAILED, document PENDING or FAILED + audit, in ONE transaction
Every stage is idempotent, so a retried job simply runs again.

Later phases append stages (OCR, layout, classification, extraction, ...) to `build_stages`.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import SYSTEM_REQUEST, AuditAction, record_audit_event
from docintel.core.logging import get_logger
from docintel.db.models import (
    ActorType,
    AuditOutcome,
    Document,
    DocumentStatus,
    DocumentVersion,
    JobStatus,
)
from docintel.documents.validation import FileKind
from docintel.processing.inspection import DocumentInspection, InspectionError, inspect_file
from docintel.storage import DocumentStorage, ObjectNotFoundError
from docintel.workers.queue import ClaimedJob

logger = get_logger(__name__)

_HASH_CHUNK = 1024 * 1024
_ERROR_MESSAGE_LIMIT = 500


class PermanentProcessingError(Exception):
    """A failure that retrying cannot fix (corrupt file, integrity mismatch, missing blob).

    The message is shown to users, so it must not contain internal details.
    """


@dataclass(slots=True)
class ProcessingContext:
    job: ClaimedJob
    document_id: Any
    version_id: Any
    storage_key: str
    sha256: str
    file_kind: FileKind
    page_count: int
    workdir: Path
    local_file: Path | None = None
    inspection: DocumentInspection | None = None
    stage_timings: dict[str, float] = field(default_factory=dict)


class Stage(Protocol):
    name: str

    async def run(self, context: ProcessingContext) -> None: ...


class IntegrityStage:
    """Fetch the stored blob and verify it is byte-identical to what was uploaded."""

    name = "integrity"

    def __init__(self, storage: DocumentStorage) -> None:
        self._storage = storage

    async def run(self, context: ProcessingContext) -> None:
        destination = context.workdir / f"original.{context.file_kind.value}"
        try:
            await self._storage.download_to(context.storage_key, destination)
        except ObjectNotFoundError as exc:
            msg = "The stored file is missing."
            raise PermanentProcessingError(msg) from exc
        digest = await asyncio.to_thread(_sha256_file, destination)
        if digest != context.sha256:
            logger.error(
                "processing.integrity_mismatch",
                document_id=str(context.document_id),
                expected=context.sha256,
                actual=digest,
            )
            msg = "The stored file failed its integrity check."
            raise PermanentProcessingError(msg)
        context.local_file = destination


class InspectStage:
    """Per-page analysis: text layer present (NATIVE) or OCR required."""

    name = "inspect"

    async def run(self, context: ProcessingContext) -> None:
        if context.local_file is None:
            msg = "inspect stage requires the integrity stage"
            raise RuntimeError(msg)
        try:
            inspection = await asyncio.to_thread(
                inspect_file, context.local_file, context.file_kind
            )
        except InspectionError as exc:
            msg = "The file could not be read during inspection."
            raise PermanentProcessingError(msg) from exc
        if inspection.page_count != context.page_count:
            logger.warning(
                "processing.page_count_mismatch",
                document_id=str(context.document_id),
                upload_pages=context.page_count,
                inspected_pages=inspection.page_count,
            )
        context.inspection = inspection


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


async def _lock_document(session: AsyncSession, document_id: Any) -> Document | None:
    """Row-lock only `documents` (eager-loaded relations are outer joins, which can't be locked)."""
    document: Document | None = await session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update(of=Document)
        .execution_options(populate_existing=True)
    )
    return document


def build_stages(storage: DocumentStorage) -> list[Stage]:
    return [IntegrityStage(storage), InspectStage()]


class DocumentProcessingHandler:
    """Job handler for JobType.DOCUMENT_PROCESSING."""

    def __init__(self, storage: DocumentStorage, stages: list[Stage] | None = None) -> None:
        self._stages = stages if stages is not None else build_stages(storage)

    async def prepare(self, session: AsyncSession, job: ClaimedJob) -> ProcessingContext | None:
        """Load the target and mark the document PROCESSING. None = nothing to do (deleted)."""
        row = (
            await session.execute(
                select(Document, DocumentVersion)
                .join(DocumentVersion, DocumentVersion.id == job.document_version_id)
                .where(Document.id == job.document_id)
                .with_for_update(of=Document)
            )
        ).first()
        if row is None:
            return None
        document, version = row
        if document.deleted_at is not None:
            return None
        document.status = DocumentStatus.PROCESSING
        return ProcessingContext(
            job=job,
            document_id=document.id,
            version_id=version.id,
            storage_key=version.storage_key,
            sha256=version.sha256,
            file_kind=FileKind(version.file_kind),
            page_count=version.page_count,
            workdir=Path(tempfile.mkdtemp(prefix="docintel-job-")),
        )

    async def execute(self, context: ProcessingContext, on_stage: Any) -> None:
        try:
            for stage in self._stages:
                await on_stage(stage.name, context.stage_timings)
                started = time.perf_counter()
                await stage.run(context)
                context.stage_timings[stage.name] = round((time.perf_counter() - started) * 1000, 2)
        finally:
            shutil.rmtree(context.workdir, ignore_errors=True)

    async def on_success(
        self, session: AsyncSession, context: ProcessingContext, finished_at: datetime
    ) -> None:
        document = await _lock_document(session, context.document_id)
        version = await session.get(DocumentVersion, context.version_id)
        if document is None or version is None:
            return
        if context.inspection is not None:
            version.inspection = context.inspection.to_json()
        # Deleted while processing: keep the results, don't resurrect the document's status.
        if document.deleted_at is None:
            document.status = DocumentStatus.COMPLETED
            document.processing_error = None
            document.last_processed_at = finished_at
        record_audit_event(
            session,
            action=AuditAction.DOCUMENT_PROCESSING_COMPLETED,
            outcome=AuditOutcome.SUCCESS,
            meta=SYSTEM_REQUEST,
            actor_type=ActorType.WORKER,
            entity_type="document",
            entity_id=context.document_id,
            details={
                "job_id": str(context.job.id),
                "attempt": context.job.attempts,
                "stage_timings_ms": context.stage_timings,
                "inspection_kind": context.inspection.kind.value if context.inspection else None,
                "pages_needing_ocr": len(context.inspection.pages_needing_ocr)
                if context.inspection
                else None,
            },
        )

    async def on_failure(
        self,
        session: AsyncSession,
        job: ClaimedJob,
        *,
        new_status: JobStatus,
        user_message: str,
    ) -> None:
        if job.document_id is None:
            return
        document = await _lock_document(session, job.document_id)
        if document is None or document.deleted_at is not None:
            return
        final = new_status == JobStatus.FAILED
        document.status = DocumentStatus.FAILED if final else DocumentStatus.PENDING
        document.processing_error = user_message[:_ERROR_MESSAGE_LIMIT] if final else None
        if final:
            document.last_processed_at = datetime.now(UTC)
            record_audit_event(
                session,
                action=AuditAction.DOCUMENT_PROCESSING_FAILED,
                outcome=AuditOutcome.FAILURE,
                meta=SYSTEM_REQUEST,
                actor_type=ActorType.WORKER,
                entity_type="document",
                entity_id=job.document_id,
                details={"job_id": str(job.id), "attempts": job.attempts, "error": user_message},
            )

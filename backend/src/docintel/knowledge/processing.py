"""Knowledge ingestion job (JobType.KNOWLEDGE_PROCESSING), run by the worker.

  prepare  -> load the knowledge document (skip if it was deleted)
  stages   -> integrity (re-hash the stored file), parse (Markdown/text directly; PDFs and
              images through the same extraction and OCR as business documents), chunk
              (section-aware), sensitivity (content scan), embed (behind the sensitivity gate)
  success  -> chunks replaced, version state decided (ACTIVE / SUPERSEDED), audit - ONE
              transaction, serialized per document key
  failure  -> retried, or FAILED with a user-safe message + audit

A failing embedding provider does not lose the document: transient errors are retried while
attempts remain, after that the chunks are stored without vectors (full-text search still finds
them) and `docintel reembed` adds the vectors later.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.ai.errors import ProviderError
from docintel.ai.routing import max_sensitivity
from docintel.audit.service import SYSTEM_REQUEST, AuditAction, record_audit_event
from docintel.core.logging import get_logger
from docintel.db.models import (
    ActorType,
    AuditOutcome,
    JobStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeFormat,
    KnowledgeStatus,
    Sensitivity,
)
from docintel.documents.validation import FileKind
from docintel.knowledge.chunking import Chunk, ChunkingOptions, chunk_source
from docintel.knowledge.embedding import NOT_CONFIGURED, ChunkEmbedder, EmbeddedTexts
from docintel.knowledge.lifecycle import (
    active_version,
    is_newer,
    lock_document_key,
    mirror_to_chunks,
    retrieval_window,
)
from docintel.knowledge.sources import ParsedSource, parse_markdown, parse_pages, parse_text
from docintel.processing.content import PageContent
from docintel.processing.extraction import extract_document
from docintel.processing.inspection import InspectionError, inspect_file
from docintel.processing.ocr import OCRError
from docintel.processing.pipeline import PermanentProcessingError, download_verified
from docintel.processing.sensitivity import SensitivityAssessment, assess_pages
from docintel.processing.services import ProcessingServices
from docintel.storage import DocumentStorage
from docintel.workers.queue import ClaimedJob

logger = get_logger(__name__)

_ERROR_LIMIT = 1000
_NOTE_LIMIT = 300
_FILE_KINDS: dict[KnowledgeFormat, FileKind] = {
    KnowledgeFormat.PDF: FileKind.PDF,
    KnowledgeFormat.PNG: FileKind.PNG,
    KnowledgeFormat.JPEG: FileKind.JPEG,
    KnowledgeFormat.TIFF: FileKind.TIFF,
}


@dataclass(slots=True)
class KnowledgeContext:
    job: ClaimedJob
    knowledge_document_id: uuid.UUID
    storage_key: str
    sha256: str
    source_format: KnowledgeFormat
    title: str
    title_from_content: bool
    sensitivity: Sensitivity
    workdir: Path
    local_file: Path | None = None
    source: ParsedSource | None = None
    pages: list[PageContent] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    assessment: SensitivityAssessment | None = None
    embedded: EmbeddedTexts | None = None
    stage_timings: dict[str, float] = field(default_factory=dict)

    @property
    def effective_sensitivity(self) -> Sensitivity:
        detected = self.assessment.detected if self.assessment else None
        return max_sensitivity(self.sensitivity, detected)


class KnowledgeProcessingHandler:
    def __init__(
        self,
        storage: DocumentStorage,
        services: ProcessingServices,
        embedder: ChunkEmbedder | None,
        chunking: ChunkingOptions,
    ) -> None:
        self._storage = storage
        self._services = services
        self._embedder = embedder
        self._chunking = chunking

    # ------------------------------------------------------------------------ prepare
    async def prepare(self, session: AsyncSession, job: ClaimedJob) -> KnowledgeContext | None:
        if job.knowledge_document_id is None:
            return None
        document: KnowledgeDocument | None = await session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == job.knowledge_document_id)
            .with_for_update(of=KnowledgeDocument)
        )
        if document is None or document.deleted_at is not None:
            return None
        document.status = KnowledgeStatus.PROCESSING
        return KnowledgeContext(
            job=job,
            knowledge_document_id=document.id,
            storage_key=document.storage_key,
            sha256=document.sha256,
            source_format=document.source_format,
            title=document.title,
            title_from_content=bool(job.payload.get("title_from_content")),
            sensitivity=document.sensitivity,
            workdir=Path(tempfile.mkdtemp(prefix="docintel-knowledge-")),
        )

    # ------------------------------------------------------------------------ stages
    async def execute(self, context: KnowledgeContext, on_stage: Any) -> None:
        stages = (
            ("integrity", self._integrity),
            ("parse", self._parse),
            ("chunk", self._chunk),
            ("embed", self._embed),
        )
        try:
            for name, run in stages:
                await on_stage(name, context.stage_timings)
                started = time.perf_counter()
                await run(context)
                context.stage_timings[name] = round((time.perf_counter() - started) * 1000, 2)
        finally:
            shutil.rmtree(context.workdir, ignore_errors=True)

    async def _integrity(self, context: KnowledgeContext) -> None:
        destination = context.workdir / "original"
        await download_verified(self._storage, context.storage_key, context.sha256, destination)
        context.local_file = destination

    async def _parse(self, context: KnowledgeContext) -> None:
        if context.local_file is None:
            msg = "parse requires the integrity stage"
            raise RuntimeError(msg)
        if context.source_format in (KnowledgeFormat.MARKDOWN, KnowledgeFormat.TEXT):
            raw = await asyncio.to_thread(context.local_file.read_text, encoding="utf-8-sig")
            text = raw.replace("\r\n", "\n").replace("\r", "\n")
            markdown = context.source_format == KnowledgeFormat.MARKDOWN
            parser = parse_markdown if markdown else parse_text
            try:
                context.source = parser(text)
            except ValueError as exc:
                msg = "The document's front matter is invalid."
                raise PermanentProcessingError(msg) from exc
            context.assessment = assess_pages([(1, text)])
            return
        kind = _FILE_KINDS[context.source_format]
        try:
            inspection = await asyncio.to_thread(inspect_file, context.local_file, kind)
        except InspectionError as exc:
            msg = "The file could not be read during inspection."
            raise PermanentProcessingError(msg) from exc
        try:
            context.pages = await extract_document(
                context.local_file,
                kind,
                inspection,
                ocr=self._services.ocr,
                options=self._services.extraction,
                workdir=context.workdir,
            )
        except OCRError as exc:
            if exc.retryable:
                raise
            msg = "Text recognition is not available. Contact an administrator."
            raise PermanentProcessingError(msg) from exc
        context.source = parse_pages(context.pages)
        context.assessment = assess_pages((page.page_number, page.text) for page in context.pages)

    async def _chunk(self, context: KnowledgeContext) -> None:
        if context.source is None:
            msg = "chunk requires the parse stage"
            raise RuntimeError(msg)
        title = context.title
        if context.title_from_content and context.source.title:
            title = context.source.title[:300]
            context.title = title
        context.chunks = chunk_source(context.source, self._chunking, title=title)
        if not context.chunks:
            msg = "No text could be extracted from the document."
            raise PermanentProcessingError(msg)

    async def _embed(self, context: KnowledgeContext) -> None:
        if self._embedder is None:
            context.embedded = EmbeddedTexts(None, None, NOT_CONFIGURED)
            return
        texts = [chunk.context_text for chunk in context.chunks]
        try:
            context.embedded = await self._embedder.embed_documents(
                texts, context.effective_sensitivity
            )
        except (ProviderError, ValueError) as exc:
            retryable = isinstance(exc, ProviderError) and exc.retryable
            if retryable and context.job.attempts < context.job.max_attempts:
                raise
            logger.warning(
                "knowledge.embedding_failed",
                knowledge_document_id=str(context.knowledge_document_id),
                error_type=type(exc).__name__,
            )
            context.embedded = EmbeddedTexts(
                None,
                None,
                "not embedded: the embedding provider failed "
                "(full-text search only; run `docintel reembed`)",
            )

    # ------------------------------------------------------------------------ results
    def _chunk_rows(
        self,
        document: KnowledgeDocument,
        context: KnowledgeContext,
        successor: KnowledgeDocument | None,
    ) -> list[KnowledgeChunk]:
        embedded = context.embedded
        vectors = embedded.vectors if embedded is not None else None
        model = embedded.model if embedded is not None and vectors is not None else None
        sensitivity = context.effective_sensitivity
        start, end = retrieval_window(document, successor)
        return [
            KnowledgeChunk(
                knowledge_document_id=document.id,
                chunk_index=chunk.index,
                context_prefix=chunk.context_prefix,
                content=chunk.content,
                section_path=chunk.breadcrumb,
                heading=chunk.heading[:300],
                kind=chunk.kind,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                token_count=chunk.token_count,
                content_hash=chunk.content_hash,
                embedding=vectors[index] if vectors is not None else None,
                embedding_model=model,
                status=document.status,
                department_id=document.department_id,
                category=document.category,
                sensitivity=sensitivity,
                effective_from=start,
                effective_to=end,
            )
            for index, chunk in enumerate(context.chunks)
        ]

    async def on_success(
        self, session: AsyncSession, context: KnowledgeContext, finished_at: datetime
    ) -> None:
        key: str | None = await session.scalar(
            select(KnowledgeDocument.document_key).where(
                KnowledgeDocument.id == context.knowledge_document_id
            )
        )
        if key is None:
            return
        await lock_document_key(session, key)
        document: KnowledgeDocument | None = await session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == context.knowledge_document_id)
            .with_for_update(of=KnowledgeDocument)
            .execution_options(populate_existing=True)
        )
        if document is None or document.deleted_at is not None:
            return  # deleted while processing: nothing becomes searchable

        await session.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.knowledge_document_id == document.id)
        )
        current = await active_version(session, key, exclude=document.id)
        document.processed_at = finished_at
        document.title = context.title
        successor: KnowledgeDocument | None = None
        superseded: KnowledgeDocument | None = None
        if current is None:
            document.status = KnowledgeStatus.ACTIVE
        elif is_newer(document.effective_from, current.effective_from):
            current.status = KnowledgeStatus.SUPERSEDED
            await session.flush()  # free the one-active-version index first
            document.status = KnowledgeStatus.ACTIVE
            document.supersedes_id = current.id
            superseded = current
        else:
            document.status = KnowledgeStatus.SUPERSEDED  # an older version, kept for history
            successor = current

        embedded = context.embedded
        document.page_count = len(context.pages) or None
        document.chunk_count = len(context.chunks)
        document.effective_sensitivity = context.effective_sensitivity
        document.embedding_model = (
            embedded.model if embedded is not None and embedded.vectors is not None else None
        )
        document.embedding_note = (embedded.note if embedded is not None else None) or None
        if document.embedding_note:
            document.embedding_note = document.embedding_note[:_NOTE_LIMIT]
        document.processing_error = None
        session.add_all(self._chunk_rows(document, context, successor))
        if superseded is not None:
            await mirror_to_chunks(session, superseded, document)

        detected = context.assessment.detected if context.assessment else None
        record_audit_event(
            session,
            action=AuditAction.KNOWLEDGE_PROCESSING_COMPLETED,
            outcome=AuditOutcome.SUCCESS,
            meta=SYSTEM_REQUEST,
            actor_type=ActorType.WORKER,
            entity_type="knowledge_document",
            entity_id=document.id,
            details={
                "job_id": str(context.job.id),
                "attempt": context.job.attempts,
                "stage_timings_ms": context.stage_timings,
                "status": document.status.value,
                "superseded": str(superseded.id) if superseded else None,
                "chunks": document.chunk_count,
                "pages": document.page_count,
                "embedding_model": document.embedding_model,
                "embedding_note": document.embedding_note,
                "detected_sensitivity": detected.value if detected else None,
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
        if job.knowledge_document_id is None:
            return
        document: KnowledgeDocument | None = await session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == job.knowledge_document_id)
            .with_for_update(of=KnowledgeDocument)
        )
        if document is None or document.deleted_at is not None:
            return
        if new_status != JobStatus.FAILED:
            return  # retried: it stays PROCESSING
        document.status = KnowledgeStatus.FAILED
        document.processing_error = user_message[:_ERROR_LIMIT]
        document.processed_at = datetime.now(UTC)
        record_audit_event(
            session,
            action=AuditAction.KNOWLEDGE_PROCESSING_FAILED,
            outcome=AuditOutcome.FAILURE,
            meta=SYSTEM_REQUEST,
            actor_type=ActorType.WORKER,
            entity_type="knowledge_document",
            entity_id=job.knowledge_document_id,
            details={"job_id": str(job.id), "attempts": job.attempts, "error": user_message},
        )

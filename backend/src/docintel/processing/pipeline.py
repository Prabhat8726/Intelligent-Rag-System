"""Document processing pipeline (worker side).

Stages do file and compute work only; database writes happen in short transactions owned by the
handler:
  prepare  -> document PROCESSING (or skip if deleted)
  stages   -> integrity (re-hash the stored blob), inspect (per-page native vs OCR),
              extract (text layer / OCR, layout, tables), previews (page images to storage),
              classify (local model, gated LLM fallback, sensitivity assessment),
              fields (structured extraction with evidence, normalization and confidence)
  success  -> pages, tables, classification, extraction, review reasons, document status, job
              COMPLETED and audit, in ONE transaction
  failure  -> job retried or FAILED, document PENDING or FAILED + audit, in ONE transaction
Every stage is idempotent (results are replaced per document version), so a retried job simply
runs again.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import SYSTEM_REQUEST, AuditAction, record_audit_event
from docintel.classification.service import ClassificationOutcome
from docintel.core.logging import get_logger
from docintel.db.models import (
    ActorType,
    AuditOutcome,
    ClassificationMethod,
    Document,
    DocumentClassification,
    DocumentExtraction,
    DocumentPage,
    DocumentStatus,
    DocumentTable,
    DocumentType,
    DocumentVersion,
    ExtractionMethod,
    JobStatus,
    ReviewReason,
    Sensitivity,
    TableMethod,
    TableRow,
)
from docintel.documents.validation import FileKind
from docintel.fields.service import ExtractionOutcome, ExtractionRequest
from docintel.fields.store import carry_over_corrections, extraction_record
from docintel.processing.content import DocumentTable as ExtractedTable
from docintel.processing.content import PageContent
from docintel.processing.extraction import extract_document
from docintel.processing.inspection import (
    DocumentInspection,
    InspectionError,
    PageMethod,
    inspect_file,
)
from docintel.processing.ocr import OCRError
from docintel.processing.sensitivity import SensitivityAssessment, assess_pages
from docintel.processing.services import ProcessingServices
from docintel.processing.tables import stitch_tables
from docintel.storage import DocumentStorage, ObjectNotFoundError, page_preview_key
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
    version_number: int
    storage_key: str
    sha256: str
    file_kind: FileKind
    page_count: int
    sensitivity: Sensitivity
    workdir: Path
    local_file: Path | None = None
    inspection: DocumentInspection | None = None
    pages: list[PageContent] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    assessment: SensitivityAssessment | None = None
    classification: ClassificationOutcome | None = None
    # A reviewer's label wins over the classifier (and picks the extraction schema).
    human_label: DocumentType | None = None
    extraction: ExtractionOutcome | None = None
    review_reasons: list[ReviewReason] = field(default_factory=list)
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


class ExtractStage:
    """Text per page (PDF text layer or OCR), layout, tables (stitched across pages)."""

    name = "extract"

    def __init__(self, services: ProcessingServices) -> None:
        self._services = services

    async def run(self, context: ProcessingContext) -> None:
        if context.local_file is None or context.inspection is None:
            msg = "extract stage requires integrity and inspect"
            raise RuntimeError(msg)
        try:
            pages = await extract_document(
                context.local_file,
                context.file_kind,
                context.inspection,
                ocr=self._services.ocr,
                options=self._services.extraction,
                workdir=context.workdir,
            )
        except OCRError as exc:
            if exc.retryable:
                raise
            msg = "Text recognition is not available. Contact an administrator."
            raise PermanentProcessingError(msg) from exc
        context.pages = pages
        context.tables = stitch_tables(pages)
        threshold = self._services.ocr_review_below_confidence
        if any("ocr_failed" in page.warnings for page in pages):
            context.review_reasons.append(ReviewReason.OCR_FAILED)
        if any(
            page.method == PageMethod.OCR
            and page.ocr_confidence is not None
            and page.ocr_confidence < threshold
            for page in pages
        ):
            context.review_reasons.append(ReviewReason.LOW_OCR_CONFIDENCE)


class PreviewStage:
    """Upload the page images rendered during extraction."""

    name = "previews"

    def __init__(self, storage: DocumentStorage) -> None:
        self._storage = storage

    async def run(self, context: ProcessingContext) -> None:
        for page in context.pages:
            if page.preview_file is None or not page.preview_file.is_file():
                continue
            key = page_preview_key(context.document_id, context.version_number, page.page_number)
            await self._storage.put_file(key, page.preview_file, content_type="image/png")
            page.preview_key = key


class ClassifyStage:
    """Sensitivity assessment of the text, then two-stage classification behind the AI gate."""

    name = "classify"

    def __init__(self, services: ProcessingServices) -> None:
        self._services = services

    async def run(self, context: ProcessingContext) -> None:
        assessment = assess_pages((page.page_number, page.text) for page in context.pages)
        text = "\n\n".join(page.text for page in context.pages)
        outcome = await self._services.classifier.classify(
            text,
            declared=context.sensitivity,
            assessment=assessment,
            document_id=context.document_id,
            allow_llm=context.human_label is None,
        )
        context.classification = outcome
        context.assessment = assessment.with_type(outcome.label)
        if outcome.review_reason is not None:
            context.review_reasons.append(outcome.review_reason)


class FieldsStage:
    """Structured extraction for the document's type (the reviewer's label if there is one)."""

    name = "fields"

    def __init__(self, services: ProcessingServices) -> None:
        self._services = services

    async def run(self, context: ProcessingContext) -> None:
        classification = context.classification
        label = context.human_label or (classification.label if classification else None)
        uncertain = context.human_label is None and (
            classification is None or classification.review_reason is not None
        )
        outcome = await self._services.fields.extract(
            ExtractionRequest(
                document_type=label,
                pages=context.pages,
                tables=context.tables,
                declared=context.sensitivity,
                detected=context.assessment.detected if context.assessment else None,
                document_id=context.document_id,
                classification_uncertain=uncertain,
            )
        )
        context.extraction = outcome
        if outcome is not None:
            context.review_reasons.extend(outcome.scoring.reasons)


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


def build_stages(storage: DocumentStorage, services: ProcessingServices) -> list[Stage]:
    return [
        IntegrityStage(storage),
        InspectStage(),
        ExtractStage(services),
        PreviewStage(storage),
        ClassifyStage(services),
        FieldsStage(services),
    ]


def _page_row(version_id: Any, page: PageContent) -> DocumentPage:
    confidence = None if page.ocr_confidence is None else Decimal(f"{page.ocr_confidence:.2f}")
    return DocumentPage(
        document_version_id=version_id,
        page_number=page.page_number,
        width=round(page.width, 2),
        height=round(page.height, 2),
        unit=page.unit,
        rotation_applied=page.rotation_applied,
        extraction_method=ExtractionMethod(page.method.value),
        ocr_confidence=confidence,
        word_count=len(page.words),
        text=page.text,
        words=[word.to_list() for word in page.words],
        layout=page.layout_json(),
        preview_storage_key=page.preview_key,
        preview_width=page.preview_size[0] if page.preview_size else None,
        preview_height=page.preview_size[1] if page.preview_size else None,
    )


def _table_row(version_id: Any, index: int, table: ExtractedTable) -> DocumentTable:
    return DocumentTable(
        document_version_id=version_id,
        table_index=index,
        page_start=table.page_start,
        page_end=table.page_end,
        header=table.header,
        bbox=table.bbox.to_list(),
        extraction_method=TableMethod(table.method),
        confidence=Decimal(f"{table.confidence:.4f}"),
        row_count=len(table.rows),
        rows=[
            TableRow(
                row_index=row_index,
                page_number=row.page_number,
                cells=row.cells,
                bbox=row.bbox.to_list(),
            )
            for row_index, row in enumerate(table.rows)
        ],
    )


class DocumentProcessingHandler:
    """Job handler for JobType.DOCUMENT_PROCESSING."""

    def __init__(
        self, storage: DocumentStorage, stages: list[Stage], services: ProcessingServices
    ) -> None:
        self._stages = stages
        self._services = services

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
        human_label = await session.scalar(
            select(DocumentClassification.label).where(
                DocumentClassification.document_id == document.id,
                DocumentClassification.is_current.is_(True),
                DocumentClassification.method == ClassificationMethod.HUMAN,
            )
        )
        return ProcessingContext(
            job=job,
            document_id=document.id,
            version_id=version.id,
            version_number=version.version_number,
            storage_key=version.storage_key,
            sha256=version.sha256,
            file_kind=FileKind(version.file_kind),
            page_count=version.page_count,
            sensitivity=document.sensitivity,
            workdir=Path(tempfile.mkdtemp(prefix="docintel-job-")),
            human_label=human_label,
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

    async def _store_classification(
        self, session: AsyncSession, document: Document, context: ProcessingContext
    ) -> DocumentClassification | None:
        """Record the machine classification; a human label stays current if there is one."""
        outcome = context.classification
        if outcome is None or outcome.label is None:
            return None
        current = await session.scalar(
            select(DocumentClassification).where(
                DocumentClassification.document_id == document.id,
                DocumentClassification.is_current.is_(True),
            )
        )
        human_current = current is not None and current.method == ClassificationMethod.HUMAN
        if current is not None and not human_current:
            current.is_current = False
            await session.flush()  # free the "one current row" index before inserting
        record = DocumentClassification(
            document_id=document.id,
            document_version_id=context.version_id,
            label=outcome.label,
            confidence=Decimal(f"{outcome.confidence:.4f}"),
            method=outcome.method,
            model_version=outcome.model_version,
            signals=outcome.signals,
            is_current=not human_current,
        )
        session.add(record)
        if human_current:
            context.review_reasons = [
                reason
                for reason in context.review_reasons
                if reason != ReviewReason.CLASSIFICATION_UNCERTAIN
            ]
            return current
        document.document_type = outcome.label
        document.type_confidence = record.confidence
        return record

    async def _store_extraction(
        self, session: AsyncSession, document: Document, context: ProcessingContext
    ) -> DocumentExtraction | None:
        """Make this run's extraction current; corrections of the same version carry over."""
        previous = await session.scalar(
            select(DocumentExtraction).where(
                DocumentExtraction.document_id == document.id,
                DocumentExtraction.is_current.is_(True),
            )
        )
        if previous is not None:
            previous.is_current = False
            await session.flush()  # free the "one current row" index before inserting
        outcome = context.extraction
        if outcome is None:
            document.vendor_id = None
            return None
        same_version = previous is not None and previous.document_version_id == context.version_id
        corrections = carry_over_corrections(
            previous if same_version else None, outcome, self._services.fields.policy
        )
        if corrections:
            # Corrections may settle what made the run uncertain: use the rescored reasons.
            stale = {
                ReviewReason.EXTRACTION_FAILED,
                ReviewReason.MISSING_REQUIRED_FIELDS,
                ReviewReason.EXTRACTION_UNCERTAIN,
                ReviewReason.EXTRACTION_INCONSISTENT,
            }
            context.review_reasons = [r for r in context.review_reasons if r not in stale]
            context.review_reasons.extend(outcome.scoring.reasons)
        record = extraction_record(
            document_id=document.id,
            version_id=context.version_id,
            outcome=outcome,
            corrections=corrections,
        )
        session.add(record)
        vendor_field = next(
            (item for item in outcome.fields if item.name == "vendor_name" and item.group is None),
            None,
        )
        corrected_vendor = (
            vendor_field.corrected_normalized.get("vendor")
            if vendor_field is not None
            and vendor_field.corrected
            and vendor_field.corrected_normalized
            else None
        )
        if corrected_vendor:
            document.vendor_id = uuid.UUID(str(corrected_vendor["vendor_id"]))
        else:
            document.vendor_id = outcome.vendor.vendor_id if outcome.vendor else None
        return record

    async def on_success(
        self, session: AsyncSession, context: ProcessingContext, finished_at: datetime
    ) -> None:
        document = await _lock_document(session, context.document_id)
        version = await session.get(DocumentVersion, context.version_id)
        if document is None or version is None:
            return
        if context.inspection is not None:
            version.inspection = context.inspection.to_json()
        if context.assessment is not None:
            version.sensitivity_assessment = context.assessment.to_json()

        # Replace this version's results (reprocessing is idempotent).
        await session.execute(
            delete(DocumentPage).where(DocumentPage.document_version_id == context.version_id)
        )
        await session.execute(
            delete(DocumentTable).where(DocumentTable.document_version_id == context.version_id)
        )
        session.add_all(_page_row(context.version_id, page) for page in context.pages)
        session.add_all(
            _table_row(context.version_id, index, table)
            for index, table in enumerate(context.tables)
        )
        current = await self._store_classification(session, document, context)
        extraction = await self._store_extraction(session, document, context)

        reasons = list(dict.fromkeys(reason.value for reason in context.review_reasons))
        # Deleted while processing: keep the results, don't resurrect the document's status.
        if document.deleted_at is None:
            document.status = (
                DocumentStatus.REVIEW_REQUIRED if reasons else DocumentStatus.COMPLETED
            )
            document.review_reasons = reasons
            document.processing_error = None
            document.last_processed_at = finished_at
        outcome = context.classification
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
                "pages": len(context.pages),
                "ocr_pages": sum(1 for page in context.pages if page.method == PageMethod.OCR),
                "tables": len(context.tables),
                "document_type": current.label.value if current else None,
                "classification_method": current.method.value if current else None,
                "llm_used": bool(outcome and outcome.signals.get("llm", {}).get("used")),
                "extraction": None
                if extraction is None
                else {
                    "schema": extraction.schema_name,
                    "status": extraction.status.value,
                    "method": extraction.method.value,
                    "confidence": str(extraction.overall_confidence),
                    "review_level": extraction.review_level.value,
                    "llm_used": bool(
                        context.extraction and context.extraction.signals.get("llm", {}).get("used")
                    ),
                },
                "review_reasons": reasons,
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

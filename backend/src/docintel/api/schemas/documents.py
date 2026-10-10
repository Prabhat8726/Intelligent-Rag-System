"""Document API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, computed_field

from docintel.api.schemas.auth import DepartmentRead
from docintel.api.schemas.common import RequestModel, ResponseModel
from docintel.api.schemas.vendors import VendorSummary
from docintel.db.models import (
    ClassificationMethod,
    DocumentSource,
    DocumentStatus,
    DocumentType,
    ExtractionMethod,
    JobStatus,
    JobType,
    ReviewLevelValue,
    ReviewPriority,
    ReviewTaskStatus,
    ReviewTaskType,
    Sensitivity,
    TableMethod,
)


class UserSummary(ResponseModel):
    id: uuid.UUID
    full_name: str


class DocumentVersionRead(ResponseModel):
    id: uuid.UUID
    version_number: int
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    page_count: int
    created_at: datetime


class ProcessingJobRead(ResponseModel):
    id: uuid.UUID
    job_type: JobType
    status: JobStatus
    attempts: int
    max_attempts: int
    stage: str | None
    stage_timings: dict[str, Any]
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


class ReviewTaskBrief(ResponseModel):
    """The open review task of a document (inbox and detail)."""

    id: uuid.UUID
    task_type: ReviewTaskType
    status: ReviewTaskStatus
    priority: ReviewPriority
    due_at: datetime | None
    assigned_to: UserSummary | None


class ExtractionBrief(ResponseModel):
    """The current extraction's overall confidence and review routing (inbox)."""

    overall_confidence: Decimal
    review_level: ReviewLevelValue


class DocumentRead(ResponseModel):
    id: uuid.UUID
    display_filename: str
    document_type: DocumentType | None
    type_confidence: Decimal | None
    status: DocumentStatus
    sensitivity: Sensitivity
    source: DocumentSource
    owner: UserSummary
    department: DepartmentRead | None
    duplicate_of_id: uuid.UUID | None
    duplicate_reason: str | None
    processing_error: str | None
    review_reasons: list[str] = Field(
        default_factory=list, description="Why the document needs review (REVIEW_REQUIRED)"
    )
    last_processed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    current_version: DocumentVersionRead | None
    vendor: VendorSummary | None = Field(
        default=None, description="Vendor master entry the extracted vendor resolved to"
    )
    review: ReviewTaskBrief | None = Field(
        default=None,
        validation_alias="open_review_task",
        description="The open review task (present exactly while the status is REVIEW_REQUIRED)",
    )
    extraction: ExtractionBrief | None = Field(
        default=None, description="The current extraction (lists only; null before extraction)"
    )


class ClassificationRead(ResponseModel):
    id: uuid.UUID
    label: DocumentType
    confidence: Decimal
    method: ClassificationMethod
    model_version: str | None
    signals: dict[str, Any] = Field(
        description="Evidence: local model probabilities, keywords, LLM use and gate decision"
    )
    note: str | None
    created_by: UserSummary | None
    is_current: bool
    created_at: datetime


class PageSummary(ResponseModel):
    page_number: int
    width: float
    height: float
    unit: str = Field(description="'pt' (PDF points) or 'px' (image pixels); boxes use the same")
    rotation_applied: int
    extraction_method: ExtractionMethod
    ocr_confidence: Decimal | None
    word_count: int
    preview_storage_key: str | None = Field(default=None, exclude=True)
    preview_width: int | None
    preview_height: int | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_preview(self) -> bool:
        return self.preview_storage_key is not None


class PageDetail(PageSummary):
    text: str
    words: list[list[Any]] = Field(
        description="[text, x0, y0, x1, y1, ocr_confidence | null, size], top-left origin"
    )
    layout: dict[str, Any] = Field(description="lines (column segments) and reading-order blocks")


class TableRowRead(ResponseModel):
    row_index: int
    page_number: int
    cells: list[str]
    bbox: list[float]


class TableRead(ResponseModel):
    id: uuid.UUID
    table_index: int
    page_start: int
    page_end: int
    header: list[str]
    bbox: list[float]
    extraction_method: TableMethod
    confidence: Decimal
    row_count: int
    rows: list[TableRowRead]


class DocumentDetail(DocumentRead):
    inspection: dict[str, Any] | None = Field(
        default=None, description="Per-page analysis from the worker's inspection stage"
    )
    sensitivity_assessment: dict[str, Any] | None = Field(
        default=None, description="Content findings and the sensitivity they imply (AI gate)"
    )
    latest_job: ProcessingJobRead | None = None
    classification: ClassificationRead | None = None
    classification_history: list[ClassificationRead] = Field(default_factory=list)
    pages: list[PageSummary] = Field(default_factory=list)


class ClassificationCorrection(RequestModel):
    document_type: DocumentType
    note: str | None = Field(default=None, max_length=500)


class DocumentPage(ResponseModel):
    items: list[DocumentRead]
    total: int
    limit: int
    offset: int

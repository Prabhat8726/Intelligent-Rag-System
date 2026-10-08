"""Document API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, computed_field

from docintel.api.schemas.auth import DepartmentRead
from docintel.api.schemas.common import ResponseModel
from docintel.db.models import (
    DocumentSource,
    DocumentStatus,
    DocumentType,
    JobStatus,
    JobType,
    Sensitivity,
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
    last_processed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    current_version: DocumentVersionRead | None


class DocumentDetail(DocumentRead):
    inspection: dict[str, Any] | None = Field(
        default=None, description="Per-page analysis from the worker's inspection stage"
    )
    latest_job: ProcessingJobRead | None = None


class DocumentPage(ResponseModel):
    items: list[DocumentRead]
    total: int
    limit: int
    offset: int

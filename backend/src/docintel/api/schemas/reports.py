"""Report API schemas (Module 30)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from docintel.api.schemas.common import RequestModel, ResponseModel
from docintel.db.models import ReportSubject, ReportType


class ReportCreate(RequestModel):
    report_type: ReportType
    subject_id: uuid.UUID = Field(
        description="A document (invoice verification, contract review, compliance review), a "
        "comparison (document comparison) or an investigation (AI analysis)"
    )


class ReportSummary(ResponseModel):
    id: uuid.UUID
    report_type: ReportType
    subject_type: ReportSubject
    subject_id: uuid.UUID
    title: str
    document_ids: list[uuid.UUID]
    workflow_id: uuid.UUID | None
    template_version: int
    content_sha256: str
    as_of: datetime = Field(description="Newest timestamp among the records shown")
    generated_by_email: str
    created_at: datetime


class ReportRead(ReportSummary):
    content: str = Field(description="Markdown, rendered from the snapshot")
    snapshot: dict[str, Any]


class ReportPage(ResponseModel):
    items: list[ReportSummary]
    total: int
    limit: int
    offset: int


class ReportVerification(ResponseModel):
    report_id: uuid.UUID
    matches: bool = Field(description="Rendering the stored snapshot gives the stored content")
    content_sha256: str

"""Knowledge base API schemas (Module 12)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from docintel.api.schemas.auth import DepartmentRead
from docintel.api.schemas.common import ResponseModel
from docintel.api.schemas.documents import ProcessingJobRead, UserSummary
from docintel.db.models import KnowledgeCategory, KnowledgeFormat, KnowledgeStatus, Sensitivity


class KnowledgeDocumentRead(ResponseModel):
    id: uuid.UUID
    document_key: str
    title: str
    category: KnowledgeCategory
    version_label: str | None
    department: DepartmentRead | None  # None = organization-wide
    sensitivity: Sensitivity
    effective_sensitivity: Sensitivity | None
    effective_from: date | None
    effective_to: date | None
    status: KnowledgeStatus
    supersedes_id: uuid.UUID | None
    source_format: KnowledgeFormat
    original_filename: str
    size_bytes: int
    page_count: int | None
    chunk_count: int
    embedding_model: str | None
    embedding_note: str | None
    processing_error: str | None
    processed_at: datetime | None
    uploaded_by: UserSummary
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentDetail(KnowledgeDocumentRead):
    sha256: str
    mime_type: str
    latest_job: ProcessingJobRead | None = None


class KnowledgeDocumentPage(ResponseModel):
    items: list[KnowledgeDocumentRead]
    total: int
    limit: int
    offset: int


class KnowledgeChunkRead(ResponseModel):
    id: uuid.UUID
    chunk_index: int
    section_path: str
    heading: str
    kind: str
    content: str
    page_start: int | None
    page_end: int | None
    token_count: int
    embedding_model: str | None
    has_embedding: bool
    effective_from: date | None
    effective_to: date | None

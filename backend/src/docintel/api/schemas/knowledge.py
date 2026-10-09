"""Knowledge base API schemas (Module 12)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field

from docintel.api.schemas.auth import DepartmentRead
from docintel.api.schemas.common import QueryText, RequestModel, ResponseModel
from docintel.api.schemas.documents import ProcessingJobRead, UserSummary
from docintel.db.models import KnowledgeCategory, KnowledgeFormat, KnowledgeStatus, Sensitivity
from docintel.knowledge.answering import AnswerStatus


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


# ------------------------------------------------------------------------------ retrieval
class KnowledgeScopeInput(RequestModel):
    as_of: date | None = Field(
        default=None, description="Cite the versions in force on this date (default: today)"
    )
    categories: list[KnowledgeCategory] = Field(default_factory=list, max_length=6)
    document_keys: list[str] = Field(default_factory=list, max_length=20)


class KnowledgeSearchRequest(KnowledgeScopeInput):
    query: QueryText = Field(min_length=1, max_length=1000)
    top_k: int | None = Field(default=None, ge=1, le=20)


class KnowledgeQueryRequest(KnowledgeScopeInput):
    question: QueryText = Field(min_length=3, max_length=1000)


class PassageRead(ResponseModel):
    chunk_id: uuid.UUID
    knowledge_document_id: uuid.UUID
    document_key: str
    title: str
    version_label: str | None
    category: KnowledgeCategory
    status: KnowledgeStatus
    section_path: str
    heading: str
    content: str
    page_start: int | None
    page_end: int | None
    effective_from: date | None
    effective_to: date | None
    score: float
    dense_similarity: float | None
    text_score: float | None
    term_coverage: float


class EvidenceRead(ResponseModel):
    sufficient: bool
    term_coverage: float
    dense_similarity: float | None
    reason: str


class RetrievalInfo(ResponseModel):
    mode: str
    embedding_model: str | None
    as_of: date
    query_terms: list[str]
    timings_ms: dict[str, float]


class KnowledgeSearchResponse(ResponseModel):
    query: str
    passages: list[PassageRead]
    evidence: EvidenceRead
    retrieval: RetrievalInfo


class SourceRead(ResponseModel):
    label: str
    cited: bool
    sent_to_model: bool
    knowledge_document_id: uuid.UUID
    document_key: str
    title: str
    version_label: str | None
    status: KnowledgeStatus
    section_path: str
    page_start: int | None
    page_end: int | None
    effective_from: date | None
    effective_to: date | None
    chunk_ids: list[uuid.UUID]
    content: str


class ClaimRead(ResponseModel):
    text: str
    citations: list[str]
    grounded: bool
    grounding: float


class KnowledgeAnswerResponse(ResponseModel):
    question: str
    status: AnswerStatus
    answer: str | None
    claims: list[ClaimRead]
    sources: list[SourceRead]
    evidence: EvidenceRead
    retrieval: RetrievalInfo
    notices: list[str]
    model: str | None
    provider: str | None

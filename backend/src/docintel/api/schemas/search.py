"""Business document search API schemas (Module 28)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import Field

from docintel.api.schemas.common import QueryText, RequestModel, ResponseModel
from docintel.api.schemas.documents import DocumentRead
from docintel.db.models import DocumentType


class DocumentSearchRequest(RequestModel):
    query: QueryText = Field(
        min_length=1,
        max_length=500,
        examples=[
            "invoices from Kestrel Industrial Supply",
            "contracts containing termination clauses",
            "documents with payment terms longer than 60 days",
        ],
    )
    document_types: list[DocumentType] = Field(default_factory=list, max_length=9)
    limit: int = Field(default=20, ge=1, le=100)


class ComparisonRead(ResponseModel):
    op: str = Field(description="gt, gte, lt, lte or eq")
    value: Decimal


class SearchInterpretation(ResponseModel):
    """How the request was understood: every filter that was applied, and the free text."""

    document_types: list[DocumentType]
    vendor: str | None
    vendors_matched: list[str] = Field(description="Vendor master entries the name matched")
    payment_terms_days: ComparisonRead | None
    total: ComparisonRead | None
    date_from: date | None
    date_to: date | None
    text: str = Field(description="Searched in the documents' text (full text and vectors)")
    recognized: list[str]


class SnippetRead(ResponseModel):
    chunk_id: uuid.UUID
    text: str
    page_start: int | None
    page_end: int | None


class SearchHitRead(ResponseModel):
    document: DocumentRead
    vendor_name: str | None
    document_date: date | None
    total: Decimal | None
    payment_terms_days: int | None
    score: float | None
    reasons: list[str]
    snippet: SnippetRead | None


class DocumentSearchResponse(ResponseModel):
    query: str
    mode: str = Field(description="structured, text or structured+text")
    interpretation: SearchInterpretation
    total: int
    results: list[SearchHitRead]

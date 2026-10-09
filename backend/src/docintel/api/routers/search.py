"""Search over business documents (Module 28)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from docintel.api.deps import RagDep, SessionDep, SettingsDep, require_permission
from docintel.api.schemas.common import PROBLEM_RESPONSES
from docintel.api.schemas.documents import DocumentRead
from docintel.api.schemas.search import (
    ComparisonRead,
    DocumentSearchRequest,
    DocumentSearchResponse,
    SearchHitRead,
    SearchInterpretation,
    SnippetRead,
)
from docintel.auth.permissions import Permission
from docintel.db.models import User
from docintel.search.query import Comparison
from docintel.search.service import DocumentSearchService

router = APIRouter(prefix="/search", tags=["search"], responses=PROBLEM_RESPONSES)

Reader = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_READ))]


def _comparison(value: Comparison | None) -> ComparisonRead | None:
    return None if value is None else ComparisonRead(op=value.op, value=value.value)


@router.post(
    "",
    response_model=DocumentSearchResponse,
    summary="Search documents in natural language: filters on extracted data plus text search",
    description="Understood without a model: document types (invoices, purchase orders, "
    "delivery notes, contracts, ...), a vendor ('from <vendor>'), payment terms ('payment "
    "terms longer than 60 days', 'net 30'), totals ('over 10,000'), dates ('in March 2026', "
    "'after 2026-01-01'). The rest is searched in the documents' text. `interpretation` "
    "shows what was applied. Only documents the caller may read are searched.",
)
async def search_documents(
    body: DocumentSearchRequest,
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    rag: RagDep,
) -> DocumentSearchResponse:
    service = DocumentSearchService(
        session, rag.embedder, min_similarity=settings.rag_min_dense_similarity
    )
    results = await service.search(
        user, body.query, limit=body.limit, document_types=body.document_types
    )
    parsed = results.parsed
    return DocumentSearchResponse(
        query=body.query,
        mode=results.mode,
        total=results.total,
        interpretation=SearchInterpretation(
            document_types=list(dict.fromkeys([*body.document_types, *parsed.document_types])),
            vendor=parsed.vendor,
            vendors_matched=results.vendors,
            payment_terms_days=_comparison(parsed.payment_terms_days),
            total=_comparison(parsed.total),
            date_from=parsed.date_from,
            date_to=parsed.date_to,
            text=parsed.text,
            recognized=list(parsed.recognized),
        ),
        results=[
            SearchHitRead(
                document=DocumentRead.model_validate(hit.document),
                vendor_name=hit.vendor_name,
                document_date=hit.document_date,
                total=hit.total,
                payment_terms_days=hit.payment_terms_days,
                score=hit.score,
                reasons=hit.reasons,
                snippet=None
                if hit.snippet is None
                else SnippetRead(
                    chunk_id=hit.snippet.chunk_id,
                    text=hit.snippet.text,
                    page_start=hit.snippet.page_start,
                    page_end=hit.snippet.page_end,
                ),
            )
            for hit in results.hits
        ],
    )

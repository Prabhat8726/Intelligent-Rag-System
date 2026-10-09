"""Knowledge base endpoints (Modules 12, 13)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from sqlalchemy import select

from docintel.api.deps import (
    RagDep,
    RequestMetaDep,
    SessionDep,
    SettingsDep,
    StorageDep,
    require_permission,
)
from docintel.api.schemas.common import PROBLEM_RESPONSES, ProblemDetail
from docintel.api.schemas.documents import ProcessingJobRead
from docintel.api.schemas.knowledge import (
    ClaimRead,
    EvidenceRead,
    KnowledgeAnswerResponse,
    KnowledgeChunkRead,
    KnowledgeDocumentDetail,
    KnowledgeDocumentPage,
    KnowledgeDocumentRead,
    KnowledgeQueryRequest,
    KnowledgeScopeInput,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    PassageRead,
    RetrievalInfo,
    SourceRead,
)
from docintel.auth.permissions import Permission
from docintel.db.models import (
    KnowledgeCategory,
    KnowledgeStatus,
    ProcessingJob,
    Sensitivity,
    User,
)
from docintel.knowledge.rag import KnowledgeQueryService, today
from docintel.knowledge.retrieval import KnowledgeScope, Passage, Retrieval
from docintel.knowledge.service import KnowledgeFilters, KnowledgeService
from docintel.knowledge.validation import MetadataInput

router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
    responses={
        **PROBLEM_RESPONSES,
        404: {"model": ProblemDetail, "description": "Not found or not accessible"},
    },
)

Reader = Annotated[User, Depends(require_permission(Permission.KNOWLEDGE_READ))]
Manager = Annotated[User, Depends(require_permission(Permission.KNOWLEDGE_MANAGE))]


@router.post(
    "/documents",
    status_code=status.HTTP_201_CREATED,
    response_model=KnowledgeDocumentRead,
    summary="Add a knowledge document or a new version of one (Markdown, text, PDF, image)",
    description="Metadata comes from the form fields, then from a front-matter block of "
    "`key: value` lines in Markdown/text files (title, document_key, version, category, "
    "effective_from, effective_to, sensitivity, department). A file with the `document_key` of "
    "an existing document becomes its new version once processed; the previous version is then "
    "SUPERSEDED and is no longer cited for dates after the new one takes effect.",
    responses={
        409: {"model": ProblemDetail, "description": "Duplicate, scope or processing conflict"},
        413: {"model": ProblemDetail, "description": "File too large"},
        415: {"model": ProblemDetail, "description": "Unsupported or mismatched file type"},
    },
)
async def upload_knowledge_document(
    response: Response,
    user: Manager,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    meta: RequestMetaDep,
    file: Annotated[UploadFile, File(description="The knowledge document")],
    title: Annotated[str | None, Form(max_length=300)] = None,
    document_key: Annotated[str | None, Form(max_length=100)] = None,
    category: Annotated[KnowledgeCategory | None, Form()] = None,
    version_label: Annotated[str | None, Form(max_length=50)] = None,
    sensitivity: Annotated[Sensitivity | None, Form()] = None,
    effective_from: Annotated[date | None, Form()] = None,
    effective_to: Annotated[date | None, Form()] = None,
    department_id: Annotated[
        uuid.UUID | None,
        Form(description="Restrict to one department (your own; any for administrators)"),
    ] = None,
) -> KnowledgeDocumentRead:
    form = MetadataInput(
        title=title,
        document_key=document_key,
        category=category,
        version_label=version_label,
        sensitivity=sensitivity,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    document = await KnowledgeService(session, storage, settings).upload(
        actor=user, upload=file, form=form, department_id=department_id, meta=meta
    )
    response.headers["Location"] = f"/api/v1/knowledge/documents/{document.id}"
    return KnowledgeDocumentRead.model_validate(document)


@router.get(
    "/documents",
    response_model=KnowledgeDocumentPage,
    summary="List knowledge documents and versions visible to the caller",
)
async def list_knowledge_documents(
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    status_filter: Annotated[KnowledgeStatus | None, Query(alias="status")] = None,
    category: KnowledgeCategory | None = None,
    document_key: Annotated[str | None, Query(max_length=100)] = None,
    q: Annotated[str | None, Query(max_length=200, description="Title contains")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> KnowledgeDocumentPage:
    filters = KnowledgeFilters(
        status=status_filter, category=category, document_key=document_key, title_contains=q
    )
    items, total = await KnowledgeService(session, storage, settings).list(
        user, filters, limit=limit, offset=offset
    )
    return KnowledgeDocumentPage(
        items=[KnowledgeDocumentRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/documents/{document_id}",
    response_model=KnowledgeDocumentDetail,
    summary="Knowledge document detail with its latest processing job",
)
async def get_knowledge_document(
    document_id: uuid.UUID,
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> KnowledgeDocumentDetail:
    document = await KnowledgeService(session, storage, settings).get(user, document_id)
    job = await session.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.knowledge_document_id == document.id)
        .order_by(ProcessingJob.created_at.desc())
        .limit(1)
    )
    detail = KnowledgeDocumentDetail.model_validate(document)
    detail.latest_job = ProcessingJobRead.model_validate(job) if job else None
    return detail


@router.get(
    "/documents/{document_id}/chunks",
    response_model=list[KnowledgeChunkRead],
    summary="The passages a knowledge document was split into (what RAG retrieves and cites)",
)
async def list_knowledge_chunks(
    document_id: uuid.UUID,
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> list[KnowledgeChunkRead]:
    chunks = await KnowledgeService(session, storage, settings).chunks(user, document_id)
    return [
        KnowledgeChunkRead(
            id=chunk.id,
            chunk_index=chunk.chunk_index,
            section_path=chunk.section_path,
            heading=chunk.heading,
            kind=chunk.kind,
            content=chunk.content,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            token_count=chunk.token_count,
            embedding_model=chunk.embedding_model,
            has_embedding=chunk.embedding is not None,
            effective_from=chunk.effective_from,
            effective_to=chunk.effective_to,
        )
        for chunk in chunks
    ]


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive a knowledge document version (its passages are no longer retrieved)",
    description="Archiving the active version makes the latest earlier version active again.",
)
async def archive_knowledge_document(
    document_id: uuid.UUID,
    user: Manager,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    meta: RequestMetaDep,
) -> Response:
    await KnowledgeService(session, storage, settings).archive(user, document_id, meta)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------ retrieval
def _scope(body: KnowledgeScopeInput) -> KnowledgeScope:
    return KnowledgeScope(
        as_of=body.as_of or today(),
        categories=tuple(dict.fromkeys(body.categories)),
        document_keys=tuple(dict.fromkeys(key.strip() for key in body.document_keys)),
    )


def _passage(passage: Passage) -> PassageRead:
    return PassageRead(
        chunk_id=passage.chunk_id,
        knowledge_document_id=passage.knowledge_document_id,
        document_key=passage.document_key,
        title=passage.title,
        version_label=passage.version_label,
        category=passage.category,
        status=passage.status,
        section_path=passage.section_path,
        heading=passage.heading,
        content=passage.content,
        page_start=passage.page_start,
        page_end=passage.page_end,
        effective_from=passage.effective_from,
        effective_to=passage.effective_to,
        score=round(passage.score, 6),
        dense_similarity=None
        if passage.dense_similarity is None
        else round(passage.dense_similarity, 4),
        text_score=None if passage.text_score is None else round(passage.text_score, 4),
        term_coverage=round(passage.term_coverage, 4),
    )


def _evidence(retrieval: Retrieval) -> EvidenceRead:
    evidence = retrieval.evidence
    return EvidenceRead(
        sufficient=evidence.sufficient,
        term_coverage=round(evidence.term_coverage, 4),
        dense_similarity=None
        if evidence.dense_similarity is None
        else round(evidence.dense_similarity, 4),
        reason=evidence.reason,
    )


def _retrieval_info(retrieval: Retrieval) -> RetrievalInfo:
    return RetrievalInfo(
        mode=retrieval.mode,
        embedding_model=retrieval.embedding_model,
        as_of=retrieval.as_of,
        query_terms=retrieval.query_terms,
        timings_ms=retrieval.timings_ms,
    )


@router.post(
    "/search",
    response_model=KnowledgeSearchResponse,
    summary="Hybrid (vector + full-text) search over the knowledge base, without generation",
)
async def search_knowledge(
    body: KnowledgeSearchRequest, user: Reader, session: SessionDep, rag: RagDep
) -> KnowledgeSearchResponse:
    retrieval = await KnowledgeQueryService(session, rag).search(
        user, body.query, _scope(body), top_k=body.top_k
    )
    return KnowledgeSearchResponse(
        query=body.query,
        passages=[_passage(passage) for passage in retrieval.passages],
        evidence=_evidence(retrieval),
        retrieval=_retrieval_info(retrieval),
    )


@router.post(
    "/query",
    response_model=KnowledgeAnswerResponse,
    summary="Answer a question from the knowledge base with source citations",
    description="Statuses: ANSWERED (every claim cites a provided source and matches it), "
    "PARTIALLY_SUPPORTED (some claims were removed or could not be matched to their sources), "
    "INSUFFICIENT_EVIDENCE (retrieval found no adequate passage; no model call is made), "
    "RETRIEVAL_ONLY (no model configured or allowed for these sources: passages only).",
)
async def query_knowledge(
    body: KnowledgeQueryRequest,
    user: Reader,
    session: SessionDep,
    rag: RagDep,
    meta: RequestMetaDep,
) -> KnowledgeAnswerResponse:
    retrieval, answer = await KnowledgeQueryService(session, rag).ask(
        user, body.question, _scope(body), meta
    )
    sent = answer.model is not None
    return KnowledgeAnswerResponse(
        question=body.question,
        status=answer.status,
        answer=answer.answer,
        claims=[
            ClaimRead(
                text=claim.text,
                citations=list(claim.citations),
                grounded=claim.grounded,
                grounding=claim.grounding,
            )
            for claim in answer.claims
        ],
        sources=[
            SourceRead(
                label=source.label,
                cited=source.label in answer.cited,
                sent_to_model=sent and source not in answer.withheld,
                knowledge_document_id=source.lead.knowledge_document_id,
                document_key=source.lead.document_key,
                title=source.lead.title,
                version_label=source.lead.version_label,
                status=source.lead.status,
                section_path=source.lead.section_path,
                page_start=min(
                    (p.page_start for p in source.passages if p.page_start is not None),
                    default=None,
                ),
                page_end=max(
                    (p.page_end for p in source.passages if p.page_end is not None), default=None
                ),
                effective_from=source.lead.effective_from,
                effective_to=source.lead.effective_to,
                chunk_ids=[passage.chunk_id for passage in source.passages],
                content=source.content,
            )
            for source in answer.sources
        ],
        evidence=_evidence(retrieval),
        retrieval=_retrieval_info(retrieval),
        notices=answer.notices,
        model=answer.model,
        provider=answer.provider,
    )

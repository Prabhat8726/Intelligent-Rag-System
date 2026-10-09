"""Knowledge base endpoints (Modules 12, 13)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from sqlalchemy import select

from docintel.api.deps import (
    RequestMetaDep,
    SessionDep,
    SettingsDep,
    StorageDep,
    require_permission,
)
from docintel.api.schemas.common import PROBLEM_RESPONSES, ProblemDetail
from docintel.api.schemas.documents import ProcessingJobRead
from docintel.api.schemas.knowledge import (
    KnowledgeChunkRead,
    KnowledgeDocumentDetail,
    KnowledgeDocumentPage,
    KnowledgeDocumentRead,
)
from docintel.auth.permissions import Permission
from docintel.db.models import (
    KnowledgeCategory,
    KnowledgeStatus,
    ProcessingJob,
    Sensitivity,
    User,
)
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

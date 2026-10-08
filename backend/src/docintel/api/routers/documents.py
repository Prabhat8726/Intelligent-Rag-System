"""Document ingestion endpoints (Modules 1-2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Path, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from docintel.api.deps import (
    RequestMetaDep,
    SessionDep,
    SettingsDep,
    StorageDep,
    require_permission,
)
from docintel.api.schemas.common import PROBLEM_RESPONSES, ProblemDetail
from docintel.api.schemas.documents import (
    ClassificationCorrection,
    ClassificationRead,
    DocumentDetail,
    DocumentPage,
    DocumentRead,
    PageDetail,
    PageSummary,
    ProcessingJobRead,
    TableRead,
)
from docintel.auth.permissions import Permission
from docintel.db.models import DocumentStatus, DocumentType, Sensitivity, User
from docintel.documents.content import DocumentContentService
from docintel.documents.service import DocumentFilters, DocumentService
from docintel.documents.validation import SUPPORTED_EXTENSIONS

router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    responses={
        **PROBLEM_RESPONSES,
        404: {"model": ProblemDetail, "description": "Not found or not accessible"},
    },
)

Reader = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_READ))]
Uploader = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_UPLOAD))]
Processor = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_PROCESS))]
Deleter = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_DELETE))]
Reviewer = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_REVIEW))]
PageNumber = Annotated[int, Path(ge=1, le=100_000)]


def _content_disposition(filename: str) -> str:
    """RFC 6266 attachment header with an ASCII fallback and a UTF-8 `filename*`."""
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=DocumentRead,
    summary="Upload a document (PDF, PNG, JPEG, TIFF) and queue it for processing",
    description=f"Supported extensions: {', '.join(SUPPORTED_EXTENSIONS)}. "
    "The file type is verified from its content. Exact duplicates (same SHA-256 among documents "
    "you can see) are accepted and flagged via `duplicate_of_id`.",
    responses={
        413: {"model": ProblemDetail, "description": "File too large"},
        415: {"model": ProblemDetail, "description": "Unsupported or mismatched file type"},
    },
)
async def upload_document(
    response: Response,
    user: Uploader,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    meta: RequestMetaDep,
    file: Annotated[UploadFile, File(description="The document file")],
    sensitivity: Annotated[Sensitivity, Form()] = Sensitivity.INTERNAL,
    department_id: Annotated[uuid.UUID | None, Form(description="Admins only")] = None,
) -> DocumentRead:
    document = await DocumentService(session, storage, settings).upload(
        actor=user, upload=file, sensitivity=sensitivity, department_id=department_id, meta=meta
    )
    response.headers["Location"] = f"/api/v1/documents/{document.id}"
    return DocumentRead.model_validate(document)


@router.get("", response_model=DocumentPage, summary="List documents visible to the caller")
async def list_documents(
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    document_type: DocumentType | None = None,
    mine: bool = False,
    q: Annotated[str | None, Query(max_length=200, description="Filename contains")] = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> DocumentPage:
    filters = DocumentFilters(
        status=status_filter,
        document_type=document_type,
        owned_by_me=mine,
        filename_contains=q,
        created_from=created_from,
        created_to=created_to,
    )
    items, total = await DocumentService(session, storage, settings).list(
        user, filters, limit=limit, offset=offset
    )
    return DocumentPage(
        items=[DocumentRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{document_id}", response_model=DocumentDetail, summary="Document detail")
async def get_document(
    document_id: uuid.UUID,
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> DocumentDetail:
    service = DocumentService(session, storage, settings)
    document = await service.get(user, document_id)
    content = DocumentContentService(session, storage)
    job = await service.latest_job(document.id)
    history = [
        ClassificationRead.model_validate(record)
        for record in await content.classifications(document)
    ]
    detail = DocumentDetail.model_validate(document)
    version = document.current_version
    detail.inspection = version.inspection if version else None
    detail.sensitivity_assessment = version.sensitivity_assessment if version else None
    detail.latest_job = ProcessingJobRead.model_validate(job) if job else None
    detail.classification = next((c for c in history if c.is_current), None)
    detail.classification_history = history
    detail.pages = [PageSummary.model_validate(page) for page in await content.pages(document)]
    return detail


@router.get(
    "/{document_id}/pages/{page_number}",
    response_model=PageDetail,
    summary="Text, words with boxes and layout of one page",
)
async def get_page(
    document_id: uuid.UUID,
    page_number: PageNumber,
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> PageDetail:
    document = await DocumentService(session, storage, settings).get(user, document_id)
    page = await DocumentContentService(session, storage).page(document, page_number)
    return PageDetail.model_validate(page)


@router.get(
    "/{document_id}/pages/{page_number}/image",
    summary="Rendered page preview (PNG)",
    response_class=StreamingResponse,
    responses={200: {"content": {"image/png": {}}}},
)
async def get_page_image(
    document_id: uuid.UUID,
    page_number: PageNumber,
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> StreamingResponse:
    document = await DocumentService(session, storage, settings).get(user, document_id)
    chunks = await DocumentContentService(session, storage).open_page_image(document, page_number)
    return StreamingResponse(
        chunks,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/{document_id}/tables",
    response_model=list[TableRead],
    summary="Tables detected in the current version (multi-page tables stitched)",
)
async def get_tables(
    document_id: uuid.UUID,
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> list[TableRead]:
    document = await DocumentService(session, storage, settings).get(user, document_id)
    tables = await DocumentContentService(session, storage).tables(document)
    return [TableRead.model_validate(table) for table in tables]


@router.patch(
    "/{document_id}/classification",
    response_model=ClassificationRead,
    summary="Correct the document type (human label; becomes training data)",
)
async def correct_classification(
    document_id: uuid.UUID,
    body: ClassificationCorrection,
    user: Reviewer,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    meta: RequestMetaDep,
) -> ClassificationRead:
    document = await DocumentService(session, storage, settings).get(user, document_id)
    record = await DocumentContentService(session, storage).correct_classification(
        user, document, body.document_type, body.note, meta
    )
    return ClassificationRead.model_validate(record)


@router.get(
    "/{document_id}/file",
    summary="Download the original file",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_document(
    document_id: uuid.UUID,
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    meta: RequestMetaDep,
) -> StreamingResponse:
    download = await DocumentService(session, storage, settings).open_download(
        user, document_id, meta
    )
    return StreamingResponse(
        download.chunks,
        media_type=download.version.mime_type,
        headers={
            "Content-Disposition": _content_disposition(download.filename),
            "Content-Length": str(download.version.size_bytes),
            # Never render user files in the API origin, even if a browser is tricked into it.
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a document (queued jobs are cancelled)",
)
async def delete_document(
    document_id: uuid.UUID,
    user: Deleter,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    meta: RequestMetaDep,
) -> Response:
    await DocumentService(session, storage, settings).soft_delete(user, document_id, meta)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{document_id}/process",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ProcessingJobRead,
    summary="Queue the current version for (re)processing",
    responses={409: {"model": ProblemDetail, "description": "Already queued or processing"}},
)
async def reprocess_document(
    document_id: uuid.UUID,
    user: Processor,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    meta: RequestMetaDep,
) -> ProcessingJobRead:
    job = await DocumentService(session, storage, settings).request_reprocess(
        user, document_id, meta
    )
    await session.refresh(job)
    return ProcessingJobRead.model_validate(job)

"""Document ingestion endpoints (Modules 1-2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
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
    DocumentDetail,
    DocumentPage,
    DocumentRead,
    ProcessingJobRead,
)
from docintel.auth.permissions import Permission
from docintel.db.models import DocumentStatus, DocumentType, Sensitivity, User
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
    job = await service.latest_job(document.id)
    detail = DocumentDetail.model_validate(document)
    detail.inspection = document.current_version.inspection if document.current_version else None
    detail.latest_job = ProcessingJobRead.model_validate(job) if job else None
    return detail


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

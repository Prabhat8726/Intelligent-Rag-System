"""Cross-document comparison endpoints (Module 9)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from docintel.api.deps import RequestMetaDep, SessionDep, SettingsDep, require_permission
from docintel.api.schemas.common import PROBLEM_RESPONSES, ProblemDetail
from docintel.api.schemas.documents import UserSummary
from docintel.api.schemas.matching import (
    ComparisonCreate,
    ComparisonDocumentRead,
    ComparisonItemRead,
    ComparisonPage,
    ComparisonRead,
    ComparisonSide,
    ComparisonSummary,
)
from docintel.auth.permissions import Permission
from docintel.comparisons.service import ComparisonFilters, ComparisonService
from docintel.db.models import (
    Comparison,
    ComparisonOrigin,
    ComparisonResult,
    ComparisonType,
    Document,
    User,
)

router = APIRouter(
    prefix="/comparisons",
    tags=["comparisons"],
    responses={
        **PROBLEM_RESPONSES,
        404: {"model": ProblemDetail, "description": "Not found or not accessible"},
    },
)
Reader = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_READ))]
Comparer = Annotated[User, Depends(require_permission(Permission.COMPARISONS_CREATE))]


def comparison_summary(
    comparison: Comparison, documents: dict[uuid.UUID, Document]
) -> ComparisonSummary:
    return ComparisonSummary(
        id=comparison.id,
        comparison_type=comparison.comparison_type,
        origin=comparison.origin,
        subject_document_id=comparison.subject_document_id,
        summary=comparison.summary,
        created_at=comparison.created_at,
        requested_by=UserSummary.model_validate(comparison.requested_by)
        if comparison.requested_by
        else None,
        documents=[
            ComparisonDocumentRead(
                document_id=link.document_id,
                role=link.role,
                position=link.position,
                document_version_id=link.document_version_id,
                extraction_id=link.extraction_id,
                display_filename=documents[link.document_id].display_filename
                if link.document_id in documents
                else "",
                document_type=documents[link.document_id].document_type
                if link.document_id in documents
                else None,
            )
            for link in comparison.documents
        ],
    )


def _item(item: ComparisonResult) -> ComparisonItemRead:
    evidence = item.evidence or {}
    return ComparisonItemRead(
        id=item.id,
        position=item.position,
        item_key=item.item_key,
        category=item.category,
        check_name=item.check_name,
        line_key=item.line_key,
        status=item.status,
        left_value=item.left_value,
        right_value=item.right_value,
        difference=item.difference,
        tolerance=item.tolerance,
        explanation=item.explanation,
        left=[ComparisonSide.model_validate(side) for side in evidence.get("left", [])],
        right=[ComparisonSide.model_validate(side) for side in evidence.get("right", [])],
    )


def comparison_read(comparison: Comparison, documents: dict[uuid.UUID, Document]) -> ComparisonRead:
    summary = comparison_summary(comparison, documents)
    return ComparisonRead(
        **summary.model_dump(),
        settings=comparison.settings,
        items=[_item(item) for item in comparison.items],
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ComparisonRead,
    summary="Compare documents you choose (invoice / purchase order / delivery notes)",
    description="Matching compares every invoice and delivery note with the purchase order it "
    "references automatically; this compares documents of your choice (e.g. an invoice with an "
    "order it does not cite). Results are stored; no rules run and no review task is created.",
)
async def create_comparison(
    body: ComparisonCreate,
    response: Response,
    user: Comparer,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> ComparisonRead:
    service = ComparisonService(session, settings)
    comparison = await service.create(
        user, [(item.document_id, item.role) for item in body.documents], meta
    )
    response.headers["Location"] = f"/api/v1/comparisons/{comparison.id}"
    return comparison_read(comparison, await service.documents([comparison]))


@router.get("", response_model=ComparisonPage, summary="Comparisons whose documents you can see")
async def list_comparisons(
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    document_id: uuid.UUID | None = None,
    comparison_type: ComparisonType | None = None,
    origin: ComparisonOrigin | None = None,
    with_issues: Annotated[
        bool, Query(description="Only comparisons with a mismatch, missing or uncertain item")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> ComparisonPage:
    service = ComparisonService(session, settings)
    items, total = await service.comparisons(
        user,
        ComparisonFilters(document_id, comparison_type, origin, with_issues),
        limit=limit,
        offset=offset,
    )
    documents = await service.documents(items)
    return ComparisonPage(
        items=[comparison_summary(item, documents) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{comparison_id}",
    response_model=ComparisonRead,
    summary="A comparison with every item and its evidence from both documents",
)
async def get_comparison(
    comparison_id: uuid.UUID, user: Reader, session: SessionDep, settings: SettingsDep
) -> ComparisonRead:
    service = ComparisonService(session, settings)
    comparison = await service.get(user, comparison_id)
    return comparison_read(comparison, await service.documents([comparison]))

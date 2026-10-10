"""Reports (Module 30): generate, list, read, download and verify."""

from __future__ import annotations

import json
import re
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from docintel.api.deps import RequestMetaDep, SessionDep, SettingsDep, require_permission
from docintel.api.schemas.common import PROBLEM_RESPONSES, ProblemDetail
from docintel.api.schemas.reports import (
    ReportCreate,
    ReportPage,
    ReportRead,
    ReportSummary,
    ReportVerification,
)
from docintel.auth.permissions import Permission
from docintel.db.models import Report, ReportType, User
from docintel.reports.service import ReportService

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    responses={
        **PROBLEM_RESPONSES,
        404: {"model": ProblemDetail, "description": "Not found or not accessible"},
    },
)
Creator = Annotated[User, Depends(require_permission(Permission.REPORTS_CREATE))]
Reader = Annotated[User, Depends(require_permission(Permission.REPORTS_READ))]
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def summary(report: Report) -> ReportSummary:
    return ReportSummary(
        id=report.id,
        report_type=report.report_type,
        subject_type=report.subject_type,
        subject_id=report.subject_id,
        title=report.title,
        document_ids=list(report.document_ids),
        workflow_id=report.workflow_id,
        template_version=report.template_version,
        content_sha256=report.content_sha256,
        as_of=report.as_of,
        generated_by_email=report.generated_by.email,
        created_at=report.created_at,
    )


def detail(report: Report) -> ReportRead:
    return ReportRead(
        **summary(report).model_dump(), content=report.content, snapshot=report.snapshot
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ReportRead,
    summary="Generate a report from the current data",
    description="The report includes source documents, extracted values with evidence, rule "
    "results, comparisons, the AI analysis, human review and workflow decisions with their "
    "history, and the time of the newest record. It is readable by whoever can read every "
    "document it includes.",
    responses={409: {"model": ProblemDetail, "description": "Subject not ready"}},
)
async def create_report(
    body: ReportCreate,
    response: Response,
    user: Creator,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> ReportRead:
    service = ReportService(session, settings)
    report = await service.generate(user, body.report_type, body.subject_id, meta=meta)
    await session.commit()
    report = await service.get(user, report.id)
    response.headers["Location"] = f"/api/v1/reports/{report.id}"
    return detail(report)


@router.get("", response_model=ReportPage, summary="Reports you can read, newest first")
async def list_reports(
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    report_type: ReportType | None = None,
    document_id: uuid.UUID | None = None,
    workflow_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> ReportPage:
    reports, total = await ReportService(session, settings).reports(
        user,
        report_type=report_type,
        document_id=document_id,
        workflow_id=workflow_id,
        limit=limit,
        offset=offset,
    )
    return ReportPage(
        items=[summary(report) for report in reports], total=total, limit=limit, offset=offset
    )


@router.get("/{report_id}", response_model=ReportRead, summary="A report with its content")
async def get_report(
    report_id: uuid.UUID, user: Reader, session: SessionDep, settings: SettingsDep
) -> ReportRead:
    return detail(await ReportService(session, settings).get(user, report_id))


@router.get(
    "/{report_id}/download",
    summary="Download the report (Markdown) or its snapshot (JSON)",
    response_class=Response,
    responses={200: {"content": {"text/markdown": {}, "application/json": {}}}},
)
async def download_report(
    report_id: uuid.UUID,
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
    fmt: Annotated[Literal["md", "json"], Query(alias="format")] = "md",
) -> Response:
    service = ReportService(session, settings)
    report = await service.get(user, report_id)
    service.downloaded(user, report, fmt, meta)
    await session.commit()
    name = _UNSAFE_FILENAME.sub("-", report.title.lower()).strip("-")[:80] or "report"
    if fmt == "json":
        body = json.dumps(report.snapshot, sort_keys=True, indent=2).encode("utf-8")
        media_type = "application/json"
    else:
        body = report.content.encode("utf-8")
        media_type = "text/markdown; charset=utf-8"
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{name}-{str(report.id)[:8]}.{fmt}"',
            "X-Content-SHA256": report.content_sha256,
        },
    )


@router.post(
    "/{report_id}/verify",
    response_model=ReportVerification,
    summary="Render the stored snapshot again and compare it with the stored report",
)
async def verify_report(
    report_id: uuid.UUID, user: Reader, session: SessionDep, settings: SettingsDep
) -> ReportVerification:
    report = await ReportService(session, settings).get(user, report_id)
    return ReportVerification(
        report_id=report.id,
        matches=ReportService.verify(report),
        content_sha256=report.content_sha256,
    )

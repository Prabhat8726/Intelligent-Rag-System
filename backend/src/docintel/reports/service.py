"""Generating, listing and reading reports (the /reports API, workflows and the agent tool).

A report is readable by whoever can read every document it includes. Its content is rendered
from the snapshot stored with it; `verify` renders the snapshot again and compares hashes.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.policies import visible_documents
from docintel.comparisons.service import ComparisonService
from docintel.core.config import Settings
from docintel.core.errors import ConflictError, NotFoundError, UnprocessableContentError
from docintel.db.models import (
    ActorType,
    AgentRun,
    AgentRunStatus,
    AuditOutcome,
    Document,
    DocumentStatus,
    DocumentType,
    Report,
    ReportSubject,
    ReportType,
    Role,
    User,
    Workflow,
)
from docintel.reports.render import normalized, render, sha256
from docintel.reports.snapshot import TEMPLATE_VERSION, SnapshotBuilder

NOT_FOUND = "Report not found."
SUBJECT_OF: dict[ReportType, ReportSubject] = {
    ReportType.INVOICE_VERIFICATION: ReportSubject.DOCUMENT,
    ReportType.CONTRACT_REVIEW: ReportSubject.DOCUMENT,
    ReportType.COMPLIANCE_REVIEW: ReportSubject.DOCUMENT,
    ReportType.DOCUMENT_COMPARISON: ReportSubject.COMPARISON,
    ReportType.AI_ANALYSIS: ReportSubject.AGENT_RUN,
}
REQUIRED_TYPE: dict[ReportType, DocumentType] = {
    ReportType.INVOICE_VERIFICATION: DocumentType.INVOICE,
    ReportType.CONTRACT_REVIEW: DocumentType.CONTRACT,
}
PROCESSED = (DocumentStatus.COMPLETED, DocumentStatus.REVIEW_REQUIRED)


def visible_reports(actor: User) -> ColumnElement[bool]:
    """Every document the report includes is visible to the actor."""
    if actor.role == Role.ADMIN:
        return true()
    visible = select(func.array_agg(Document.id)).where(visible_documents(actor)).scalar_subquery()
    condition: ColumnElement[bool] = Report.document_ids.contained_by(visible)
    return condition


class ReportService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def _document(
        self, actor: User, report_type: ReportType, subject_id: uuid.UUID
    ) -> Document:
        document: Document | None = await self._session.scalar(
            select(Document).where(Document.id == subject_id, visible_documents(actor))
        )
        if document is None:
            raise NotFoundError("Document not found.")
        required = REQUIRED_TYPE.get(report_type)
        if required is not None and document.document_type != required:
            kind = (
                document.document_type.value.lower() if document.document_type else "unclassified"
            )
            msg = (
                f"A {report_type.value.lower().replace('_', ' ')} report needs a "
                f"{required.value.lower()}; {document.display_filename} is {kind}."
            )
            raise UnprocessableContentError(msg)
        if document.status not in PROCESSED or document.current_version_id is None:
            raise ConflictError(f"{document.display_filename} has not finished processing yet.")
        return document

    async def _run(self, actor: User, run_id: uuid.UUID) -> AgentRun:
        """Investigations are personal; a workflow's investigation is as visible as its
        workflow (its document)."""
        run = await self._session.get(AgentRun, run_id)
        if run is None:
            raise NotFoundError("Investigation not found.")
        if actor.role != Role.ADMIN and run.requested_by_id != actor.id:
            shared = await self._session.scalar(
                select(Workflow.id).where(
                    Workflow.agent_run_id == run.id,
                    Workflow.document_id.in_(select(Document.id).where(visible_documents(actor))),
                )
            )
            if shared is None:
                raise NotFoundError("Investigation not found.")
        if run.status != AgentRunStatus.COMPLETED or not run.result:
            raise ConflictError("The investigation has not finished.")
        return run

    async def generate(
        self,
        actor: User,
        report_type: ReportType,
        subject_id: uuid.UUID,
        *,
        meta: RequestMeta,
        workflow_id: uuid.UUID | None = None,
        actor_type: ActorType = ActorType.USER,
    ) -> Report:
        """Snapshot, render and store a report. The caller commits."""
        builder = SnapshotBuilder(self._session, actor)
        subject = SUBJECT_OF[report_type]
        if subject == ReportSubject.DOCUMENT:
            document = await self._document(actor, report_type, subject_id)
            snapshot = await builder.document_report(report_type, document)
        elif subject == ReportSubject.COMPARISON:
            comparison = await ComparisonService(self._session, self._settings).get(
                actor, subject_id
            )
            snapshot = await builder.comparison_report(comparison)
        else:
            snapshot = await builder.analysis_report(await self._run(actor, subject_id))
        if not snapshot.document_ids:
            msg = "A report needs at least one document; this investigation has none."
            raise UnprocessableContentError(msg)
        data = normalized(snapshot.data)
        content = render(data)
        report = Report(
            id=uuid.uuid4(),
            report_type=report_type,
            subject_type=subject,
            subject_id=subject_id,
            title=snapshot.title[:300],
            document_ids=snapshot.document_ids,
            workflow_id=workflow_id,
            template_version=TEMPLATE_VERSION,
            snapshot=data,
            content=content,
            content_sha256=sha256(content),
            as_of=snapshot.as_of,
            generated_by_id=actor.id,
        )
        self._session.add(report)
        await self._session.flush()
        record_audit_event(
            self._session,
            action=AuditAction.REPORT_GENERATED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            actor_type=actor_type,
            entity_type="report",
            entity_id=report.id,
            details={
                "report_type": report_type.value,
                "subject_type": subject.value,
                "subject_id": str(subject_id),
                "documents": [str(document_id) for document_id in snapshot.document_ids],
                "sha256": report.content_sha256,
                "workflow_id": str(workflow_id) if workflow_id else None,
            },
        )
        return report

    async def reports(
        self,
        actor: User,
        *,
        report_type: ReportType | None = None,
        document_id: uuid.UUID | None = None,
        workflow_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Report], int]:
        conditions: list[ColumnElement[bool]] = [visible_reports(actor)]
        if report_type is not None:
            conditions.append(Report.report_type == report_type)
        if document_id is not None:
            conditions.append(Report.document_ids.contains([document_id]))
        if workflow_id is not None:
            conditions.append(Report.workflow_id == workflow_id)
        total = await self._session.scalar(
            select(func.count()).select_from(Report).where(*conditions)
        )
        rows = await self._session.scalars(
            select(Report)
            .where(*conditions)
            .order_by(Report.created_at.desc(), Report.id)
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def get(self, actor: User, report_id: uuid.UUID) -> Report:
        report: Report | None = await self._session.scalar(
            select(Report).where(Report.id == report_id, visible_reports(actor))
        )
        if report is None:
            raise NotFoundError(NOT_FOUND)
        return report

    @staticmethod
    def verify(report: Report) -> bool:
        """Rendering the stored snapshot again gives exactly the stored content."""
        rendered = render(report.snapshot)
        return rendered == report.content and sha256(rendered) == report.content_sha256

    def downloaded(self, actor: User, report: Report, fmt: str, meta: RequestMeta) -> None:
        record_audit_event(
            self._session,
            action=AuditAction.REPORT_DOWNLOADED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="report",
            entity_id=report.id,
            details={"format": fmt, "sha256": report.content_sha256},
        )

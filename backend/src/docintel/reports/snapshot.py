"""What a report shows, gathered as JSON in a fixed order (Module 30).

A snapshot holds source documents, extracted values with their evidence, rule results,
comparisons, the AI analysis, review decisions and workflow decisions with their history - all
as the requesting user is allowed to see them. Lists are sorted and timestamps come from the
records themselves, so unchanged data always gives the same snapshot (and the same report).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from docintel.auth.policies import visible_documents
from docintel.db.models import (
    AgentRun,
    BusinessRule,
    Comparison,
    ComparisonDocument,
    ComparisonItemStatus,
    ComparisonOrigin,
    Document,
    DocumentPage,
    DocumentVersion,
    ReportType,
    ReviewRequest,
    ReviewTask,
    RuleResultRecord,
    User,
    Workflow,
    WorkflowAction,
)
from docintel.matching.store import current_extraction
from docintel.versions.clauses import compare_clauses, segment, summarize

TEMPLATE_VERSION = 1
MAX_FIELDS = 40
MAX_ITEMS = 60
MAX_FINDINGS = 30
TEXT_LIMIT = 500
ISSUE_STATUSES = (
    ComparisonItemStatus.MISMATCH,
    ComparisonItemStatus.MISSING,
    ComparisonItemStatus.UNCERTAIN,
)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def plain(value: Any) -> Any:
    """JSON-compatible, stable representation."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "value") and isinstance(value.value, str):  # enums
        return value.value
    return value


def clip(text: str | None, limit: int = TEXT_LIMIT) -> str | None:
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(slots=True)
class Snapshot:
    title: str
    data: dict[str, Any]
    document_ids: list[uuid.UUID]
    as_of: datetime


class SnapshotBuilder:
    def __init__(self, session: AsyncSession, actor: User) -> None:
        self._session = session
        self._actor = actor
        self._stamps: list[datetime] = []
        self._users: dict[uuid.UUID, str] = {}

    def _stamp(self, *values: datetime | None) -> None:
        self._stamps.extend(value for value in values if value is not None)

    async def _user(self, user_id: uuid.UUID | None) -> str | None:
        if user_id is None:
            return None
        if user_id not in self._users:
            email = await self._session.scalar(select(User.email).where(User.id == user_id))
            self._users[user_id] = email or "(unknown user)"
        return self._users[user_id]

    # ------------------------------------------------------------------ pieces
    async def document(self, document: Document, role: str) -> dict[str, Any]:
        version = (
            await self._session.get(DocumentVersion, document.current_version_id)
            if document.current_version_id
            else None
        )
        self._stamp(document.created_at, document.last_processed_at)
        return {
            "id": str(document.id),
            "role": role,
            "filename": document.display_filename,
            "document_type": plain(document.document_type),
            "status": plain(document.status),
            "sensitivity": plain(document.sensitivity),
            "version_number": version.version_number if version else None,
            "sha256": version.sha256 if version else None,
            "uploaded_by": await self._user(version.uploaded_by_id if version else None),
            "uploaded_at": iso(version.created_at if version else document.created_at),
            "number_key": document.number_key,
            "document_date": plain(document.document_date),
            "total": plain(document.total_amount),
            "currency": document.currency,
            "processed_at": iso(document.last_processed_at),
        }

    async def fields(self, document: Document) -> list[dict[str, Any]]:
        record = await current_extraction(self._session, document.id)
        if record is None:
            return []
        self._stamp(record.created_at)
        rows = sorted(
            (row for row in record.fields if row.group_name is None),
            key=lambda row: row.position,
        )
        return [
            {
                "field": row.field_path,
                "value": clip(row.corrected_value if row.corrected_at else row.original_value),
                "corrected": row.corrected_at is not None,
                "confidence": round(float(row.confidence), 3),
                "evidence": row.evidence_status.value,
                "page": row.page_number,
                "quote": clip(row.source_text, 200),
            }
            for row in rows[:MAX_FIELDS]
        ]

    async def rules(self, document: Document) -> list[dict[str, Any]]:
        rows = (
            await self._session.execute(
                select(RuleResultRecord, BusinessRule.name)
                .join(BusinessRule, BusinessRule.id == RuleResultRecord.rule_id)
                .where(
                    RuleResultRecord.document_id == document.id,
                    RuleResultRecord.document_version_id == document.current_version_id,
                )
                .order_by(RuleResultRecord.rule_code)
            )
        ).all()
        result = []
        for record, name in rows:
            self._stamp(record.evaluated_at)
            result.append(
                {
                    "code": record.rule_code,
                    "name": name,
                    "version": record.rule_version,
                    "outcome": record.outcome.value,
                    "severity": record.severity.value,
                    "message": clip(record.message),
                }
            )
        return result

    async def comparison(self, comparison: Comparison) -> dict[str, Any]:
        self._stamp(comparison.created_at)
        members = []
        for member in sorted(comparison.documents, key=lambda item: item.position):
            document = await self._session.get(Document, member.document_id)
            members.append(
                {
                    "document_id": str(member.document_id),
                    "role": member.role.value,
                    "filename": document.display_filename if document else None,
                }
            )
        items = sorted(comparison.items, key=lambda item: item.position)
        return {
            "id": str(comparison.id),
            "type": comparison.comparison_type.value,
            "origin": comparison.origin.value,
            "created_at": iso(comparison.created_at),
            "requested_by": await self._user(comparison.requested_by_id),
            "summary": dict(sorted(comparison.summary.items())),
            "members": members,
            "items": [
                {
                    "check": item.check_name,
                    "line": item.line_key,
                    "status": item.status.value,
                    "left": clip(item.left_value, 120),
                    "right": clip(item.right_value, 120),
                    "explanation": clip(item.explanation, 300),
                }
                for item in items
                if item.status in ISSUE_STATUSES
            ][:MAX_ITEMS],
            "items_total": len(items),
        }

    async def latest_auto_comparison(self, document: Document) -> Comparison | None:
        hidden = (
            select(ComparisonDocument.document_id)
            .join(Document, Document.id == ComparisonDocument.document_id)
            .where(
                ComparisonDocument.comparison_id == Comparison.id,
                ~visible_documents(self._actor),
            )
            .exists()
        )
        comparison: Comparison | None = await self._session.scalar(
            select(Comparison)
            .where(
                Comparison.subject_document_id == document.id,
                Comparison.origin == ComparisonOrigin.AUTO,
                ~hidden,
            )
            .options(selectinload(Comparison.items))
            .order_by(Comparison.created_at.desc(), Comparison.id)
            .limit(1)
        )
        return comparison

    async def reviews(self, document: Document) -> dict[str, Any]:
        tasks = list(
            await self._session.scalars(
                select(ReviewTask)
                .where(ReviewTask.document_id == document.id)
                .order_by(ReviewTask.created_at, ReviewTask.id)
            )
        )
        requests = list(
            await self._session.scalars(
                select(ReviewRequest)
                .where(ReviewRequest.document_id == document.id)
                .order_by(ReviewRequest.created_at, ReviewRequest.id)
            )
        )
        for task in tasks:
            self._stamp(task.created_at, task.updated_at, task.resolved_at)
        for request in requests:
            self._stamp(request.created_at)
        return {
            "tasks": [
                {
                    "type": task.task_type.value,
                    "status": task.status.value,
                    "priority": task.priority.value,
                    "reasons": [clip(reason.get("message"), 200) for reason in task.reasons][:10],
                    "resolution": plain(task.resolution),
                    "resolved_by": await self._user(task.resolved_by_id),
                    "resolved_at": iso(task.resolved_at),
                    "note": clip(task.resolution_note),
                    "created_at": iso(task.created_at),
                }
                for task in tasks
            ],
            "requests": [
                {
                    "requested_by": await self._user(request.requested_by_id),
                    "priority": request.priority.value,
                    "reason": clip(request.reason),
                    "created_at": iso(request.created_at),
                }
                for request in requests
            ],
        }

    async def workflows(self, document: Document) -> list[dict[str, Any]]:
        rows = list(
            await self._session.scalars(
                select(Workflow)
                .where(Workflow.document_id == document.id)
                .order_by(Workflow.created_at, Workflow.id)
            )
        )
        return [await self.workflow(row) for row in rows]

    async def workflow(self, workflow: Workflow) -> dict[str, Any]:
        self._stamp(workflow.created_at, workflow.finished_at)
        actions = list(
            await self._session.scalars(
                select(WorkflowAction)
                .where(WorkflowAction.workflow_id == workflow.id)
                .order_by(WorkflowAction.created_at, WorkflowAction.id)
                .execution_options(populate_existing=True)
            )
        )
        return {
            "id": str(workflow.id),
            "type": workflow.workflow_type.value,
            "definition_version": workflow.definition_version,
            "status": workflow.status.value,
            "outcome": workflow.outcome,
            "trigger": workflow.trigger.value,
            "initiated_by": await self._user(workflow.initiated_by_id),
            "created_at": iso(workflow.created_at),
            "finished_at": iso(workflow.finished_at),
            "steps": [
                {"step": step.step_name, "status": step.status.value}
                for step in sorted(workflow.steps, key=lambda s: s.sequence)
            ],
            "actions": [await self.action(action) for action in actions],
        }

    async def action(self, action: WorkflowAction) -> dict[str, Any]:
        self._stamp(action.created_at, action.decided_at, action.executed_at)
        transitions = []
        for item in action.transitions:
            self._stamp(item.created_at)
            transitions.append(
                {
                    "from": plain(item.from_status),
                    "to": item.to_status.value,
                    "by": await self._user(item.actor_id) or item.actor_type.value,
                    "actor_type": item.actor_type.value,
                    "at": iso(item.created_at),
                    "reason": clip(item.reason),
                }
            )
        result = action.execution_result or {}
        return {
            "id": str(action.id),
            "type": action.action_type.value,
            "status": action.status.value,
            "risk": action.risk_level.value,
            "required_role": action.required_role,
            "proposed_by": action.proposed_by_type.value,
            "rationale": clip(action.rationale, 1000),
            "confidence": action.confidence_level,
            "decided_by": await self._user(action.decided_by_id),
            "decided_at": iso(action.decided_at),
            "decision_reason": clip(action.decision_reason),
            "executed_at": iso(action.executed_at),
            "result": {
                key: clip(str(result[key]), 1000)
                for key in sorted(result)
                if key in ("decision", "payment_reference", "note", "sent", "message")
            },
            "error": clip(action.error),
            "transitions": transitions,
        }

    def analysis(self, run: AgentRun) -> dict[str, Any] | None:
        result = run.result or {}
        if not result:
            return None
        self._stamp(run.created_at, run.finished_at)
        recommendation = result.get("recommendation") or {}
        confidence = result.get("confidence") or {}
        return {
            "run_id": str(run.id),
            "query": clip(run.query),
            "finished_at": iso(run.finished_at),
            "summary": clip(result.get("summary"), 1000),
            "summary_source": result.get("summary_source"),
            "findings": [
                {
                    "category": item.get("category"),
                    "statement": clip(item.get("statement")),
                    "evidence": list(item.get("evidence", [])),
                }
                for item in result.get("findings", [])
            ][:MAX_FINDINGS],
            "sources": [
                {
                    "label": source.get("label"),
                    "title": source.get("title"),
                    "version": source.get("version_label"),
                    "section": source.get("section_path"),
                    "effective_from": source.get("effective_from"),
                }
                for source in result.get("sources", [])
            ],
            "confidence": {
                "level": confidence.get("level"),
                "score": confidence.get("score"),
                "factors": [
                    clip(factor.get("detail"), 200) for factor in confidence.get("factors", [])
                ],
            },
            "recommendation": {
                "action": recommendation.get("action"),
                "rationale": clip(recommendation.get("rationale")),
                "source": recommendation.get("source"),
                "requires_approval": recommendation.get("requires_approval"),
            },
            "model": result.get("model"),
        }

    async def workflow_analysis(self, document: Document) -> dict[str, Any] | None:
        """The investigation of the document's latest workflow (visible with the document)."""
        run = await self._session.scalar(
            select(AgentRun)
            .join(Workflow, Workflow.agent_run_id == AgentRun.id)
            .where(Workflow.document_id == document.id, AgentRun.result.is_not(None))
            .order_by(Workflow.created_at.desc(), Workflow.id)
            .limit(1)
        )
        return self.analysis(run) if run is not None else None

    async def clauses(self, document: Document) -> dict[str, Any]:
        """Clause titles of the current version and what changed since the previous one."""
        version = (
            await self._session.get(DocumentVersion, document.current_version_id)
            if document.current_version_id
            else None
        )
        if version is None:
            return {"clauses": [], "changes": None}
        current = segment(await self._pages(version.id))
        changes = None
        if version.version_number > 1:
            previous = await self._session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == document.id,
                    DocumentVersion.version_number == version.version_number - 1,
                )
            )
            pages = await self._pages(previous.id) if previous is not None else []
            if pages:
                diffs = compare_clauses(segment(pages), current)
                changes = {
                    "from_version": version.version_number - 1,
                    "to_version": version.version_number,
                    "summary": summarize(diffs),
                    "changed": [
                        {"title": diff.title, "change": diff.change.value}
                        for diff in diffs
                        if diff.change.value != "UNCHANGED"
                    ],
                }
        return {
            "clauses": [
                {"number": clause.number, "title": clause.title}
                for clause in current
                if clause.number is not None
            ],
            "changes": changes,
        }

    async def _pages(self, version_id: uuid.UUID) -> list[str]:
        return list(
            await self._session.scalars(
                select(DocumentPage.text)
                .where(DocumentPage.document_version_id == version_id)
                .order_by(DocumentPage.page_number)
            )
        )

    # ------------------------------------------------------------------ report types
    def _finish(self, title: str, data: dict[str, Any], ids: Iterable[uuid.UUID]) -> Snapshot:
        as_of = max(self._stamps) if self._stamps else datetime.fromtimestamp(0).astimezone()
        data = {"template_version": TEMPLATE_VERSION, "title": title, "as_of": iso(as_of), **data}
        return Snapshot(title, data, list(dict.fromkeys(ids)), as_of)

    async def document_report(self, report_type: ReportType, document: Document) -> Snapshot:
        """INVOICE_VERIFICATION, CONTRACT_REVIEW and COMPLIANCE_REVIEW of one document."""
        subject = await self.document(document, "subject")
        documents = [subject]
        ids = [document.id]
        data: dict[str, Any] = {"report_type": report_type.value}
        if report_type == ReportType.INVOICE_VERIFICATION:
            comparison = await self.latest_auto_comparison(document)
            data["comparison"] = await self.comparison(comparison) if comparison else None
            if comparison is not None:
                related_ids = [
                    member.document_id
                    for member in sorted(comparison.documents, key=lambda m: m.position)
                    if member.document_id != document.id
                ]
                related = {
                    row.id: row
                    for row in await self._session.scalars(
                        select(Document).where(
                            Document.id.in_(related_ids), visible_documents(self._actor)
                        )
                    )
                }
                for related_id in related_ids:
                    if related_id in related:
                        documents.append(await self.document(related[related_id], "related"))
                        ids.append(related_id)
        if report_type == ReportType.CONTRACT_REVIEW:
            data["contract"] = await self.clauses(document)
        if report_type != ReportType.COMPLIANCE_REVIEW:
            data["fields"] = await self.fields(document)
        data["documents"] = documents
        data["rules"] = await self.rules(document)
        data["analysis"] = await self.workflow_analysis(document)
        data["reviews"] = await self.reviews(document)
        data["workflows"] = await self.workflows(document)
        title = {
            ReportType.INVOICE_VERIFICATION: "Invoice verification",
            ReportType.CONTRACT_REVIEW: "Contract review",
            ReportType.COMPLIANCE_REVIEW: "Compliance review",
        }[report_type]
        return self._finish(f"{title}: {document.display_filename}", data, ids)

    async def comparison_report(self, comparison: Comparison) -> Snapshot:
        compared = await self.comparison(comparison)
        members = [uuid.UUID(member["document_id"]) for member in compared["members"]]
        rows = {
            row.id: row
            for row in await self._session.scalars(select(Document).where(Document.id.in_(members)))
        }
        documents = [
            await self.document(rows[member], "subject" if index == 0 else "related")
            for index, member in enumerate(members)
            if member in rows
        ]
        names = " vs ".join(member["filename"] or "?" for member in compared["members"][:3])
        return self._finish(
            f"Document comparison: {names}",
            {
                "report_type": ReportType.DOCUMENT_COMPARISON.value,
                "documents": documents,
                "comparison": compared,
            },
            members,
        )

    async def analysis_report(self, run: AgentRun) -> Snapshot:
        result = run.result or {}
        listed = [
            uuid.UUID(str(item["document_id"]))
            for item in result.get("documents", [])
            if item.get("document_id")
        ]
        rows = {
            row.id: row
            for row in await self._session.scalars(
                select(Document).where(
                    Document.id.in_(listed or list(run.document_ids)),
                    visible_documents(self._actor),
                )
            )
        }
        ordered = list(listed or run.document_ids)
        documents = [
            await self.document(rows[document_id], "subject")
            for document_id in dict.fromkeys(ordered)
            if document_id in rows
        ]
        workflow = await self._session.scalar(
            select(Workflow).where(
                or_(
                    Workflow.agent_run_id == run.id,
                    Workflow.id == _uuid_or_none(run.options.get("workflow_id")),
                )
            )
        )
        return self._finish(
            "AI analysis: " + (clip(run.query, 120) or "investigation"),
            {
                "report_type": ReportType.AI_ANALYSIS.value,
                "documents": documents,
                "analysis": self.analysis(run),
                "requested_by": await self._user(run.requested_by_id),
                "workflow": await self.workflow(workflow) if workflow is not None else None,
            },
            list(rows),
        )


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None

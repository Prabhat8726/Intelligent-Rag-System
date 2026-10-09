"""Everything matching found for one document, as the document page shows it."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.auth.policies import visible_documents
from docintel.db.models import (
    OPEN_TASK_STATUSES,
    Comparison,
    ComparisonDocument,
    Document,
    ReviewTask,
    RuleResultRecord,
    User,
)
from docintel.review.queue import ReviewQueue

DUPLICATE_RULE_TYPE = "duplicate_document"


@dataclass(slots=True)
class Duplicate:
    kind: str
    document: Document
    direction: str  # "original": this document copies it; "copy": it copies this document
    evidence: dict[str, Any]


@dataclass(slots=True)
class Findings:
    comparisons: list[Comparison]
    rule_results: list[RuleResultRecord]
    duplicates: list[Duplicate]
    open_task: ReviewTask | None
    history: list[ReviewTask]


class FindingsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def findings(self, actor: User, document: Document) -> Findings:
        involved = exists(
            select(ComparisonDocument.document_id).where(
                ComparisonDocument.comparison_id == Comparison.id,
                ComparisonDocument.document_id == document.id,
            )
        )
        hidden = exists(
            select(ComparisonDocument.document_id)
            .join(Document, Document.id == ComparisonDocument.document_id)
            .where(ComparisonDocument.comparison_id == Comparison.id, ~visible_documents(actor))
        )
        comparisons = list(
            await self._session.scalars(
                select(Comparison)
                .where(or_(Comparison.subject_document_id == document.id, involved), ~hidden)
                .order_by(Comparison.created_at.desc())
            )
        )
        results = list(
            await self._session.scalars(
                select(RuleResultRecord)
                .where(RuleResultRecord.document_id == document.id)
                .order_by(RuleResultRecord.rule_code)
            )
        )
        history = await ReviewQueue(self._session).history(document.id)
        open_task = next((task for task in history if task.status in OPEN_TASK_STATUSES), None)
        return Findings(
            comparisons,
            results,
            await self._duplicates(actor, document, results),
            open_task,
            history,
        )

    async def _duplicates(
        self, actor: User, document: Document, results: list[RuleResultRecord]
    ) -> list[Duplicate]:
        """Originals this document copies (from the duplicate rule and the upload check) and
        documents that copy it - only those the actor can see."""
        originals: dict[uuid.UUID, tuple[str, dict[str, Any]]] = {}
        for result in results:
            for match in result.evidence.get("duplicates", []) if result.evidence else []:
                if match.get("document_id"):
                    originals.setdefault(
                        uuid.UUID(str(match["document_id"])),
                        (str(match.get("kind")), dict(match.get("evidence") or {})),
                    )
        if document.duplicate_of_id is not None and document.duplicate_of_id not in originals:
            originals[document.duplicate_of_id] = (str(document.duplicate_reason), {})
        found: list[Duplicate] = []
        if originals:
            rows = await self._session.scalars(
                select(Document).where(Document.id.in_(originals), visible_documents(actor))
            )
            for row in rows:
                kind, evidence = originals[row.id]
                found.append(Duplicate(kind, row, "original", evidence))
        copies = await self._session.scalars(
            select(Document).where(
                Document.duplicate_of_id == document.id, visible_documents(actor)
            )
        )
        found.extend(Duplicate(str(row.duplicate_reason), row, "copy", {}) for row in copies)
        return found

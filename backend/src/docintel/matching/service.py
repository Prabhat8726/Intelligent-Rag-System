"""Matching orchestration: comparisons, duplicates, rules and review tasks for a document and
everything related to it, inside the caller's transaction.

Related documents are those sharing the purchase order reference, the document number or
(possible duplicates) vendor and amount, those flagged as duplicates of it, and those it was
compared with before. They are re-evaluated too, so the order in which an invoice, its order
and its delivery notes arrive does not matter. Everything stays inside one department: a
document is never compared with one its readers may not see.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime

from sqlalchemy import Select, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.core.config import Settings
from docintel.core.logging import get_logger
from docintel.db.models import (
    Comparison,
    ComparisonDocument,
    ComparisonOrigin,
    ComparisonResult,
    Document,
    DocumentStatus,
    DocumentType,
    RuleResultRecord,
    User,
)
from docintel.matching.compare import (
    ROLE_OF,
    ComparisonOutcome,
    Tolerances,
    compare_delivery,
    compare_invoice,
    same_vendor,
)
from docintel.matching.duplicates import DuplicateKind, DuplicateMatch, find_duplicates
from docintel.matching.facts import DocumentFacts
from docintel.matching.store import apply_key_facts, load_facts, load_rules, lock_department
from docintel.review.items import document_reasons, processing_items, rule_items
from docintel.review.service import sync_review
from docintel.rules.defaults import duplicate_params, tolerances_from_rules
from docintel.rules.engine import RuleContext, RuleDefinition, RuleResult, applicable, evaluate

logger = get_logger(__name__)

PROCESSED = (DocumentStatus.COMPLETED, DocumentStatus.REVIEW_REQUIRED)
EXACT_FILE = "EXACT_FILE_HASH"


def comparison_settings(tolerances: Tolerances) -> dict[str, str | float]:
    return {
        "price_abs": str(tolerances.price_abs),
        "price_pct": str(tolerances.price_pct),
        "quantity_abs": str(tolerances.quantity_abs),
        "tax_rate_abs": str(tolerances.tax_rate_abs),
        "min_confidence": tolerances.min_confidence,
    }


def store_comparison(
    outcome: ComparisonOutcome,
    *,
    origin: ComparisonOrigin,
    department_id: uuid.UUID | None,
    tolerances: Tolerances,
    requested_by: User | None = None,
) -> Comparison:
    """A Comparison row (with documents and results) for a computed outcome."""
    record = Comparison(
        comparison_type=outcome.comparison_type,
        origin=origin,
        subject_document_id=outcome.subject.document_id,
        department_id=department_id,
        summary=outcome.summary,
        settings=comparison_settings(tolerances),
        requested_by_id=requested_by.id if requested_by else None,
        created_at=datetime.now(UTC),
    )
    participants: list[DocumentFacts] = [outcome.subject]
    if outcome.purchase_order is not None:
        participants.append(outcome.purchase_order)
    participants.extend(outcome.deliveries)
    record.documents = [
        ComparisonDocument(
            document_id=facts.document_id,
            document_version_id=facts.version_id,
            extraction_id=facts.extraction_id,
            role=ROLE_OF[facts.document_type],
            position=position,
        )
        for position, facts in enumerate(participants)
    ]
    record.items = [
        ComparisonResult(
            position=position,
            item_key=item.key[:200],
            category=item.category,
            check_name=item.check,
            line_key=item.line[:200] if item.line else None,
            status=item.status,
            left_value=item.left_value,
            right_value=item.right_value,
            difference=item.difference,
            tolerance=item.tolerance,
            evidence={
                "left": [side.to_json() for side in item.left],
                "right": [side.to_json() for side in item.right],
            },
            explanation=item.explanation,
        )
        for position, item in enumerate(outcome.items)
    ]
    return record


class MatchingService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._facts: dict[uuid.UUID, DocumentFacts | None] = {}

    # ------------------------------------------------------------------ loading
    async def _facts_of(self, document: Document) -> DocumentFacts | None:
        if document.id not in self._facts:
            self._facts[document.id] = await load_facts(self._session, document)
        return self._facts[document.id]

    def _in_department(self, document: Document) -> Select[Document]:
        return select(Document).where(
            Document.department_id.is_not_distinct_from(document.department_id),
            Document.deleted_at.is_(None),
        )

    async def _related(self, trigger: Document) -> list[Document]:
        conditions = [Document.duplicate_of_id == trigger.id]
        if trigger.po_key:
            conditions.append(Document.po_key == trigger.po_key)
        if trigger.number_key and trigger.document_type is not None:
            conditions.append(
                (Document.number_key == trigger.number_key)
                & (Document.document_type == trigger.document_type)
            )
        if trigger.total_amount is not None and trigger.document_type is not None:
            conditions.append(
                (Document.total_amount == trigger.total_amount)
                & (Document.document_type == trigger.document_type)
                & (Document.vendor_key == trigger.vendor_key)
            )
        partners = (
            select(ComparisonDocument.document_id)
            .join(Comparison, Comparison.id == ComparisonDocument.comparison_id)
            .where(
                or_(
                    Comparison.subject_document_id == trigger.id,
                    Comparison.id.in_(
                        select(ComparisonDocument.comparison_id).where(
                            ComparisonDocument.document_id == trigger.id
                        )
                    ),
                ),
                Comparison.origin == ComparisonOrigin.AUTO,
            )
        )
        conditions.append(Document.id.in_(partners))
        rows = await self._session.scalars(
            self._in_department(trigger).where(or_(*conditions), Document.id != trigger.id)
        )
        return list(rows)

    async def _documents(
        self, document: Document, document_type: DocumentType, po_key: str
    ) -> list[Document]:
        rows = await self._session.scalars(
            self._in_department(document)
            .where(
                Document.document_type == document_type,
                Document.po_key == po_key,
                Document.status.in_(PROCESSED),
                Document.id != document.id,
            )
            .order_by(Document.created_at)
        )
        return list(rows)

    async def _order_for(
        self, document: Document, facts: DocumentFacts, tolerances: Tolerances
    ) -> DocumentFacts | None:
        """The purchase order the document references: same department, same vendor first."""
        key = facts.po_key
        if not key:
            return None
        candidates = [
            found
            for found in [
                await self._facts_of(order)
                for order in await self._documents(document, DocumentType.PURCHASE_ORDER, key)
            ]
            if found is not None
        ]
        if not candidates:
            return None
        same = [c for c in candidates if same_vendor(facts, c, tolerances.vendor_similarity)]
        return (same or candidates)[-1]  # the latest upload of that order

    async def _deliveries_for(
        self, document: Document, facts: DocumentFacts
    ) -> list[DocumentFacts]:
        if not facts.po_key:
            return []
        notes = await self._documents(document, DocumentType.DELIVERY_NOTE, facts.po_key)
        return [found for note in notes if (found := await self._facts_of(note)) is not None]

    async def _duplicates(
        self, document: Document, facts: DocumentFacts, rules: Sequence[RuleDefinition]
    ) -> list[DuplicateMatch]:
        params = duplicate_params(applicable(rules, facts.document_type))
        if params is None:
            return []
        conditions = []
        if facts.number_key:
            conditions.append(Document.number_key == facts.number_key)
        if facts.total is not None:
            conditions.append(Document.total_amount == facts.total)
        candidates: list[DocumentFacts] = []
        if conditions:
            rows = await self._session.scalars(
                self._in_department(document).where(
                    or_(*conditions),
                    Document.document_type == document.document_type,
                    Document.status.in_(PROCESSED),
                    Document.id != document.id,
                )
            )
            candidates = [found for row in rows if (found := await self._facts_of(row))]
        matches = find_duplicates(
            facts,
            candidates,
            date_window_days=int(params.get("date_window_days", 7)),
            match_amount_and_date=bool(params.get("match_amount_and_date", True)),
        )
        if document.duplicate_reason == EXACT_FILE and document.duplicate_of_id is not None:
            original = await self._session.get(Document, document.duplicate_of_id)
            if original is not None and original.deleted_at is None:
                # One entry per original document: the identical file is the strongest reason.
                matches = [match for match in matches if match.document_id != str(original.id)]
                matches.insert(
                    0,
                    DuplicateMatch(
                        kind=DuplicateKind.SAME_FILE,
                        document_id=str(original.id),
                        label=original.display_filename,
                        created_at=original.created_at,
                        evidence={"reason": "byte-identical file (same SHA-256)"},
                    ),
                )
        return matches

    # ------------------------------------------------------------------ evaluation
    async def refresh(self, trigger: Document, *, actor: User | None = None) -> None:
        """Re-evaluate `trigger` and its related documents (the caller commits)."""
        await lock_department(self._session, trigger.department_id)
        rules = await load_rules(self._session)
        tolerances = tolerances_from_rules(
            rules, min_confidence=self._settings.comparison_min_confidence
        )
        facts = await self._facts_of(trigger)
        apply_key_facts(trigger, facts)
        await self._session.flush()
        if trigger.deleted_at is None:
            await self._evaluate(trigger, rules, tolerances, actor)
        for related in await self._related(trigger):
            # Lock and re-read: a job may have just claimed it (its own run will match it).
            document = await self._session.scalar(
                select(Document)
                .where(Document.id == related.id)
                .with_for_update(of=Document)
                .execution_options(populate_existing=True)
            )
            if document is None or document.deleted_at is not None:
                continue
            if document.status not in PROCESSED:
                continue
            await self._evaluate(document, rules, tolerances, actor)

    async def _evaluate(
        self,
        document: Document,
        rules: Sequence[RuleDefinition],
        tolerances: Tolerances,
        actor: User | None,
    ) -> None:
        facts = await self._facts_of(document)
        comparison: ComparisonOutcome | None = None
        duplicates: list[DuplicateMatch] = []
        results: list[RuleResult] = []
        order: DocumentFacts | None = None
        if facts is not None:
            if facts.document_type in (DocumentType.INVOICE, DocumentType.DELIVERY_NOTE):
                order = await self._order_for(document, facts, tolerances)
            if facts.document_type == DocumentType.INVOICE:
                notes = await self._deliveries_for(document, facts)
                if order is not None or notes:
                    comparison = compare_invoice(facts, order, notes, tolerances)
            elif facts.document_type == DocumentType.DELIVERY_NOTE and order is not None:
                comparison = compare_delivery(facts, order, tolerances)
            duplicates = await self._duplicates(document, facts, rules)
            results = evaluate(
                rules,
                RuleContext(
                    document=facts,
                    reference_date=date.today(),
                    comparison=comparison,
                    duplicates=duplicates,
                    order_on_file=None if facts.po_reference is None else order is not None,
                ),
            )

        await self._session.execute(
            delete(Comparison).where(
                Comparison.subject_document_id == document.id,
                Comparison.origin == ComparisonOrigin.AUTO,
            )
        )
        await self._session.execute(
            delete(RuleResultRecord).where(RuleResultRecord.document_id == document.id)
        )
        stored: Comparison | None = None
        if comparison is not None:
            stored = store_comparison(
                comparison,
                origin=ComparisonOrigin.AUTO,
                department_id=document.department_id,
                tolerances=tolerances,
            )
            self._session.add(stored)
            await self._session.flush()
        for result in results:
            self._session.add(
                RuleResultRecord(
                    document_id=document.id,
                    document_version_id=facts.version_id if facts else None,
                    comparison_id=stored.id if stored is not None and result.items else None,
                    rule_id=uuid.UUID(str(result.rule.rule_id)),
                    rule_code=result.rule.code,
                    rule_version=result.rule.version,
                    outcome=result.outcome,
                    severity=result.severity,
                    message=result.message,
                    evidence=result.evidence,
                    items=result.items,
                )
            )

        if document.duplicate_reason != EXACT_FILE:
            strong = next((match for match in duplicates if match.strong), None)
            document.duplicate_of_id = (
                uuid.UUID(strong.document_id) if strong and strong.document_id else None
            )
            document.duplicate_reason = strong.kind.value if strong else None

        processing = [
            code
            for code in document.review_reasons
            if code not in ("RULE_VIOLATION", "DUPLICATE_SUSPECTED")
        ]
        items = [
            *processing_items(processing, review_level=facts.review_level if facts else None),
            *rule_items(results),
        ]
        document.review_reasons = document_reasons(processing, items)
        await sync_review(self._session, document, items, settings=self._settings, actor=actor)
        await self._session.flush()


async def rematch(
    session: AsyncSession, settings: Settings, document_id: uuid.UUID, actor: User | None = None
) -> bool:
    """Re-run matching for one processed document in the caller's transaction (False: skipped)."""
    department_id = await session.scalar(
        select(Document.department_id).where(Document.id == document_id)
    )
    await lock_department(session, department_id)
    document = await session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update(of=Document)
        .execution_options(populate_existing=True)
    )
    if document is None or document.deleted_at is not None or document.status not in PROCESSED:
        return False
    await MatchingService(session, settings).refresh(document, actor=actor)
    return True

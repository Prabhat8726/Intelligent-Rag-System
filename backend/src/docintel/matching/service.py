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
from dataclasses import dataclass, field
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


# ------------------------------------------------------------------------------ pure decisions
def pick_order(
    facts: DocumentFacts, orders: Sequence[DocumentFacts], tolerances: Tolerances
) -> DocumentFacts | None:
    """The purchase order a document references, among the orders carrying that number
    (oldest upload first): one from the same vendor first, the latest upload of it."""
    if not orders:
        return None
    same = [order for order in orders if same_vendor(facts, order, tolerances.vendor_similarity)]
    return (same or list(orders))[-1]


def duplicate_matches(
    facts: DocumentFacts,
    candidates: Sequence[DocumentFacts],
    rules: Sequence[RuleDefinition],
    *,
    identical_file: DuplicateMatch | None = None,
) -> list[DuplicateMatch]:
    """Possible duplicates among `candidates` under the duplicate rule's parameters.

    `identical_file` is the original of a byte-identical upload: one entry per original
    document, and the identical file is the strongest reason.
    """
    params = duplicate_params(applicable(rules, facts.document_type))
    if params is None:
        return []
    matches = find_duplicates(
        facts,
        candidates,
        date_window_days=int(params.get("date_window_days", 7)),
        match_amount_and_date=bool(params.get("match_amount_and_date", True)),
    )
    if identical_file is not None:
        matches = [match for match in matches if match.document_id != identical_file.document_id]
        matches.insert(0, identical_file)
    return matches


@dataclass(slots=True)
class Assessment:
    """What matching decides for one document: its order, comparison, duplicates and rules."""

    order: DocumentFacts | None = None
    comparison: ComparisonOutcome | None = None
    duplicates: list[DuplicateMatch] = field(default_factory=list)
    results: list[RuleResult] = field(default_factory=list)


def assess(
    facts: DocumentFacts,
    *,
    orders: Sequence[DocumentFacts],
    deliveries: Sequence[DocumentFacts],
    candidates: Sequence[DocumentFacts],
    rules: Sequence[RuleDefinition],
    tolerances: Tolerances,
    reference_date: date,
    identical_file: DuplicateMatch | None = None,
) -> Assessment:
    """Compare, look for duplicates and run the rules (pure: the service loads the inputs).

    `orders` are the purchase orders carrying the number the document references and
    `deliveries` the delivery notes referencing it (both oldest first); `candidates` are
    documents of the same type that may duplicate it. The evaluation suite calls this too.
    """
    assessment = Assessment()
    if facts.document_type in (DocumentType.INVOICE, DocumentType.DELIVERY_NOTE):
        assessment.order = pick_order(facts, orders, tolerances)
    if facts.document_type == DocumentType.INVOICE:
        if assessment.order is not None or deliveries:
            assessment.comparison = compare_invoice(
                facts, assessment.order, list(deliveries), tolerances
            )
    elif facts.document_type == DocumentType.DELIVERY_NOTE and assessment.order is not None:
        assessment.comparison = compare_delivery(facts, assessment.order, tolerances)
    assessment.duplicates = duplicate_matches(
        facts, candidates, rules, identical_file=identical_file
    )
    assessment.results = evaluate(
        rules,
        RuleContext(
            document=facts,
            reference_date=reference_date,
            comparison=assessment.comparison,
            duplicates=assessment.duplicates,
            order_on_file=None if facts.po_reference is None else assessment.order is not None,
            min_confidence=tolerances.min_confidence,
        ),
    )
    return assessment


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

    async def _facts_list(self, documents: Sequence[Document]) -> list[DocumentFacts]:
        return [found for document in documents if (found := await self._facts_of(document))]

    async def _referencing(
        self, document: Document, facts: DocumentFacts, document_type: DocumentType
    ) -> list[DocumentFacts]:
        """Processed documents of a type sharing the order reference (oldest first)."""
        if not facts.po_key:
            return []
        return await self._facts_list(await self._documents(document, document_type, facts.po_key))

    async def _duplicate_candidates(
        self, document: Document, facts: DocumentFacts
    ) -> list[DocumentFacts]:
        conditions = []
        if facts.number_key:
            conditions.append(Document.number_key == facts.number_key)
        if facts.total is not None:
            conditions.append(Document.total_amount == facts.total)
        if not conditions:
            return []
        rows = await self._session.scalars(
            self._in_department(document).where(
                or_(*conditions),
                Document.document_type == document.document_type,
                Document.status.in_(PROCESSED),
                Document.id != document.id,
            )
        )
        return await self._facts_list(list(rows))

    async def _identical_file(self, document: Document) -> DuplicateMatch | None:
        if document.duplicate_reason != EXACT_FILE or document.duplicate_of_id is None:
            return None
        original = await self._session.get(Document, document.duplicate_of_id)
        if original is None or original.deleted_at is not None:
            return None
        return DuplicateMatch(
            kind=DuplicateKind.SAME_FILE,
            document_id=str(original.id),
            label=original.display_filename,
            created_at=original.created_at,
            evidence={"reason": "byte-identical file (same SHA-256)"},
        )

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
        assessment = Assessment()
        if facts is not None:
            kind = facts.document_type
            orders, deliveries, candidates = [], [], []
            if kind in (DocumentType.INVOICE, DocumentType.DELIVERY_NOTE):
                orders = await self._referencing(document, facts, DocumentType.PURCHASE_ORDER)
            if kind == DocumentType.INVOICE:
                deliveries = await self._referencing(document, facts, DocumentType.DELIVERY_NOTE)
            if duplicate_params(applicable(rules, kind)) is not None:
                candidates = await self._duplicate_candidates(document, facts)
            assessment = assess(
                facts,
                orders=orders,
                deliveries=deliveries,
                candidates=candidates,
                rules=rules,
                tolerances=tolerances,
                reference_date=date.today(),
                identical_file=await self._identical_file(document),
            )
        comparison, duplicates, results = (
            assessment.comparison,
            assessment.duplicates,
            assessment.results,
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

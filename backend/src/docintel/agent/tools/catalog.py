"""The controlled tools (Module 15). Each wraps an existing service; none adds a capability the
REST API does not already give the same user.

`generate_report` and `get_workflow_status` arrive with reports and workflows (Phase 8).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints
from sqlalchemy import select

from docintel.agent.tools.base import (
    SideEffect,
    Tool,
    ToolFailure,
    ToolInput,
    ToolOutput,
    ToolScope,
    clip,
)
from docintel.ai.routing import max_sensitivity
from docintel.auth.permissions import Permission
from docintel.auth.policies import visible_documents
from docintel.comparisons.service import ComparisonService
from docintel.core.errors import NotFoundError
from docintel.core.text import QueryText
from docintel.db.models import (
    ComparisonItemStatus,
    ComparisonRole,
    Document,
    DocumentStatus,
    DocumentType,
    DocumentVersion,
    KnowledgeCategory,
    ReviewPriority,
    RuleOutcome,
    RuleResultRecord,
    Sensitivity,
)
from docintel.documents.extraction import ExtractionService
from docintel.documents.findings import FindingsService
from docintel.fields.store import resolved_from_row
from docintel.knowledge.rag import KnowledgeQueryService, today
from docintel.knowledge.retrieval import KnowledgeScope
from docintel.matching.facts import NUMBER_FIELD
from docintel.matching.service import PROCESSED, MatchingService
from docintel.review.service import ReviewRequestService
from docintel.search.service import DocumentSearchService

FieldPath = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_]*(\[\d{1,4}\])?(\.[a-z][a-z0-9_]*)?$"
    ),
]
MAX_FIELDS = 120
MAX_COMPARISON_ITEMS = 60
SNIPPET_CHARS = 300
PASSAGE_CHARS = 1500
SOURCE_TEXT_CHARS = 500


async def _visible_document(scope: ToolScope, document_id: uuid.UUID) -> Document:
    """The same access predicate as every document read (inaccessible = not found)."""
    document = await scope.session.scalar(
        select(Document)
        .where(Document.id == document_id, visible_documents(scope.actor))
        .execution_options(populate_existing=True)
    )
    if document is None:
        raise NotFoundError("Document not found.")
    return document


async def _visible_ids(scope: ToolScope, ids: set[uuid.UUID]) -> set[uuid.UUID]:
    if not ids:
        return set()
    rows = await scope.session.scalars(
        select(Document.id).where(Document.id.in_(ids), visible_documents(scope.actor))
    )
    return set(rows)


def _decimal_text(value: Any) -> str | None:
    return None if value is None else str(value)


# ------------------------------------------------------------------------------ search_documents
class SearchDocumentsInput(ToolInput):
    query: QueryText = Field(max_length=500, description="Plain-language search request")
    document_types: list[DocumentType] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=10, ge=1, le=20)


class DocumentHit(ToolOutput):
    document_id: uuid.UUID
    filename: str
    document_type: DocumentType | None
    document_number: str | None
    order_reference: str | None = Field(description="Purchase order number it quotes")
    status: DocumentStatus
    vendor_name: str | None
    document_date: date | None
    total: str | None
    payment_terms_days: int | None
    reasons: list[str]
    snippet: str | None


class SearchDocumentsOutput(ToolOutput):
    understood_as: list[str]
    free_text: str | None
    mode: str
    total: int
    results: list[DocumentHit]


async def search_documents(scope: ToolScope, args: SearchDocumentsInput) -> SearchDocumentsOutput:
    service = DocumentSearchService(
        scope.session, scope.rag.embedder, min_similarity=scope.settings.rag_min_dense_similarity
    )
    found = await service.search(
        scope.actor, args.query, limit=args.limit, document_types=args.document_types
    )
    return SearchDocumentsOutput(
        understood_as=list(found.parsed.recognized),
        free_text=found.parsed.text or None,
        mode=found.mode,
        total=found.total,
        results=[
            DocumentHit(
                document_id=hit.document.id,
                filename=hit.document.display_filename,
                document_type=hit.document.document_type,
                document_number=clip(hit.document_number, 100),
                order_reference=clip(hit.order_reference, 100),
                status=hit.document.status,
                vendor_name=hit.vendor_name,
                document_date=hit.document_date,
                total=_decimal_text(hit.total),
                payment_terms_days=hit.payment_terms_days,
                reasons=hit.reasons[:5],
                snippet=clip(hit.snippet.text, SNIPPET_CHARS) if hit.snippet else None,
            )
            for hit in found.hits
        ],
    )


# ------------------------------------------------------------------------------ get_document
class DocumentInput(ToolInput):
    document_id: uuid.UUID


class OpenTask(ToolOutput):
    task_id: uuid.UUID
    task_type: str
    priority: str
    status: str
    findings: int


class ExtractionInfo(ToolOutput):
    schema_name: str
    status: str
    overall_confidence: float
    review_level: str


class DocumentInfo(ToolOutput):
    document_id: uuid.UUID
    filename: str
    document_type: DocumentType | None
    type_confidence: float | None
    document_number: str | None
    status: DocumentStatus
    sensitivity: Sensitivity
    effective_sensitivity: Sensitivity = Field(
        description="The higher of the label and what processing detected in the content"
    )
    vendor_name: str | None
    document_date: date | None
    total: str | None
    currency: str | None
    version_number: int | None
    page_count: int | None
    review_reasons: list[str]
    duplicate_of_id: uuid.UUID | None
    created_at: datetime
    last_processed_at: datetime | None
    extraction: ExtractionInfo | None
    open_review_task: OpenTask | None


async def effective_sensitivity(scope: ToolScope, document: Document) -> Sensitivity:
    detected: Sensitivity | None = None
    if document.current_version_id is not None:
        assessment = await scope.session.scalar(
            select(DocumentVersion.sensitivity_assessment).where(
                DocumentVersion.id == document.current_version_id
            )
        )
        if assessment and assessment.get("detected"):
            detected = Sensitivity(assessment["detected"])
    return max_sensitivity(document.sensitivity, detected)


async def get_document(scope: ToolScope, args: DocumentInput) -> DocumentInfo:
    document = await _visible_document(scope, args.document_id)
    version = (
        await scope.session.get(DocumentVersion, document.current_version_id)
        if document.current_version_id
        else None
    )
    extractions = ExtractionService(scope.session, scope.settings)
    extraction = await extractions.current(document)
    vendor = await extractions.vendor(document)
    findings = await FindingsService(scope.session).findings(scope.actor, document)
    task = findings.open_task
    number = None
    if extraction is not None and document.document_type in NUMBER_FIELD:
        field_row = next(
            (f for f in extraction.fields if f.field_path == NUMBER_FIELD[document.document_type]),
            None,
        )
        number = resolved_from_row(field_row).display_value if field_row else None
    duplicate = document.duplicate_of_id
    if duplicate is not None and not await _visible_ids(scope, {duplicate}):
        duplicate = None
    return DocumentInfo(
        document_id=document.id,
        filename=document.display_filename,
        document_type=document.document_type,
        type_confidence=float(document.type_confidence) if document.type_confidence else None,
        document_number=clip(number, 100),
        status=document.status,
        sensitivity=document.sensitivity,
        effective_sensitivity=await effective_sensitivity(scope, document),
        vendor_name=vendor.canonical_name if vendor else None,
        document_date=document.document_date,
        total=_decimal_text(document.total_amount),
        currency=document.currency,
        version_number=version.version_number if version else None,
        page_count=version.page_count if version else None,
        review_reasons=list(document.review_reasons),
        duplicate_of_id=duplicate,
        created_at=document.created_at,
        last_processed_at=document.last_processed_at,
        extraction=ExtractionInfo(
            schema_name=extraction.schema_name,
            status=extraction.status.value,
            overall_confidence=float(extraction.overall_confidence),
            review_level=extraction.review_level.value,
        )
        if extraction
        else None,
        open_review_task=OpenTask(
            task_id=task.id,
            task_type=task.task_type.value,
            priority=task.priority.value,
            status=task.status.value,
            findings=len(task.reasons),
        )
        if task
        else None,
    )


# ---------------------------------------------------------------------------- get_extracted_fields
class ExtractedFieldsInput(ToolInput):
    document_id: uuid.UUID
    field_paths: list[FieldPath] = Field(
        default_factory=list, max_length=50, description="Only these fields (default: all)"
    )
    include_line_items: bool = True


class FieldValue(ToolOutput):
    field_path: str
    value: str | None = Field(description="As printed, or the reviewer's correction")
    normalized: Any = Field(description="Typed value (number, ISO date, ...) or null")
    confidence: float
    evidence_status: str
    corrected: bool
    required: bool
    page: int | None


class ExtractedFieldsOutput(ToolOutput):
    document_id: uuid.UUID
    schema_name: str
    extraction_status: str
    overall_confidence: float
    review_level: str
    fields: list[FieldValue]
    missing_paths: list[str] = Field(description="Requested paths the extraction does not have")
    truncated: bool


async def get_extracted_fields(
    scope: ToolScope, args: ExtractedFieldsInput
) -> ExtractedFieldsOutput:
    document = await _visible_document(scope, args.document_id)
    record = await ExtractionService(scope.session, scope.settings).current(document)
    if record is None:
        raise ToolFailure(f"{document.display_filename} has no extracted fields.")
    wanted = set(args.field_paths)
    values: list[FieldValue] = []
    for row in record.fields:
        if wanted and row.field_path not in wanted:
            continue
        if not wanted and not args.include_line_items and row.group_name is not None:
            continue
        item = resolved_from_row(row)
        values.append(
            FieldValue(
                field_path=row.field_path,
                value=clip(item.display_value, 300),
                normalized=item.value if not isinstance(item.value, str) else clip(item.value, 300),
                confidence=1.0 if item.corrected else round(item.confidence, 4),
                evidence_status="HUMAN" if item.corrected else row.evidence_status.value,
                corrected=item.corrected,
                required=row.is_required,
                page=row.page_number,
            )
        )
    present = {row.field_path for row in record.fields}
    return ExtractedFieldsOutput(
        document_id=document.id,
        schema_name=record.schema_name,
        extraction_status=record.status.value,
        overall_confidence=float(record.overall_confidence),
        review_level=record.review_level.value,
        fields=values[:MAX_FIELDS],
        missing_paths=sorted(wanted - present),
        truncated=len(values) > MAX_FIELDS,
    )


# --------------------------------------------------------------------------- get_document_evidence
class EvidenceInput(ToolInput):
    document_id: uuid.UUID
    field_path: FieldPath


class EvidenceOutput(ToolOutput):
    document_id: uuid.UUID
    field_path: str
    value: str | None
    original_value: str | None
    corrected: bool
    page_number: int | None
    source_text: str | None = Field(description="Verbatim text the value was read from")
    bbox: list[float] | None
    evidence_status: str
    origin: str | None
    method: str | None
    confidence: float
    alternatives: list[str] = Field(description="Values another extractor proposed")


async def get_document_evidence(scope: ToolScope, args: EvidenceInput) -> EvidenceOutput:
    document = await _visible_document(scope, args.document_id)
    record = await ExtractionService(scope.session, scope.settings).current(document)
    if record is None:
        raise ToolFailure(f"{document.display_filename} has no extracted fields.")
    row = next((item for item in record.fields if item.field_path == args.field_path), None)
    if row is None:
        raise ToolFailure(f"No field '{args.field_path}' was extracted from this document.")
    item = resolved_from_row(row)
    return EvidenceOutput(
        document_id=document.id,
        field_path=row.field_path,
        value=clip(item.display_value, 300),
        original_value=clip(row.original_value, 300),
        corrected=item.corrected,
        page_number=row.page_number,
        source_text=clip(row.source_text, SOURCE_TEXT_CHARS),
        bbox=row.bbox,
        evidence_status=row.evidence_status.value,
        origin=row.origin.value if row.origin else None,
        method=clip(row.method, 300),
        confidence=round(float(row.confidence), 4),
        alternatives=[
            clip(str(alt.get("value")), 200) or ""
            for alt in (row.alternatives or [])[:3]
            if isinstance(alt, dict)
        ],
    )


# --------------------------------------------------------------------------- search_knowledge_base
class KnowledgeInput(ToolInput):
    query: QueryText = Field(min_length=3, max_length=500)
    categories: list[KnowledgeCategory] = Field(default_factory=list, max_length=6)
    as_of: date | None = Field(default=None, description="Versions in force on this date")
    top_k: int = Field(default=5, ge=1, le=10)


class PassageOut(ToolOutput):
    chunk_id: uuid.UUID
    knowledge_document_id: uuid.UUID
    title: str
    version_label: str | None
    status: str
    category: str
    section_path: str
    page_start: int | None
    page_end: int | None
    effective_from: date | None
    effective_to: date | None
    sensitivity: Sensitivity
    score: float
    content: str


class KnowledgeOutput(ToolOutput):
    sufficient_evidence: bool
    term_coverage: float
    dense_similarity: float | None
    mode: str
    as_of: date
    passages: list[PassageOut]


async def search_knowledge_base(scope: ToolScope, args: KnowledgeInput) -> KnowledgeOutput:
    retrieval = await KnowledgeQueryService(scope.session, scope.rag).search(
        scope.actor,
        args.query,
        KnowledgeScope(as_of=args.as_of or today(), categories=tuple(args.categories)),
        top_k=args.top_k,
    )
    return KnowledgeOutput(
        sufficient_evidence=retrieval.evidence.sufficient,
        term_coverage=round(retrieval.evidence.term_coverage, 4),
        dense_similarity=retrieval.evidence.dense_similarity,
        mode=retrieval.mode,
        as_of=retrieval.as_of,
        passages=[
            PassageOut(
                chunk_id=passage.chunk_id,
                knowledge_document_id=passage.knowledge_document_id,
                title=passage.title,
                version_label=passage.version_label,
                status=passage.status.value,
                category=passage.category.value,
                section_path=passage.section_path,
                page_start=passage.page_start,
                page_end=passage.page_end,
                effective_from=passage.effective_from,
                effective_to=passage.effective_to,
                sensitivity=passage.sensitivity,
                score=round(passage.score, 6),
                content=clip(passage.content, PASSAGE_CHARS) or "",
            )
            for passage in retrieval.passages
        ],
    )


# ------------------------------------------------------------------------------ compare_documents
class CompareMember(ToolInput):
    document_id: uuid.UUID
    role: ComparisonRole


class CompareInput(ToolInput):
    documents: list[CompareMember] = Field(min_length=2, max_length=6)


class ComparedItem(ToolOutput):
    item_key: str
    check: str
    line: str | None
    status: ComparisonItemStatus
    left_value: str | None
    right_value: str | None
    difference: dict[str, str] | None
    explanation: str


class CompareOutput(ToolOutput):
    comparison_id: uuid.UUID
    comparison_type: str
    summary: dict[str, int]
    items: list[ComparedItem]
    truncated: bool


async def compare_documents(scope: ToolScope, args: CompareInput) -> CompareOutput:
    comparison = await ComparisonService(scope.session, scope.settings).create(
        scope.actor,
        [(member.document_id, member.role) for member in args.documents],
        scope.caller.meta,
    )
    # Problems first, so a truncated list still shows every mismatch.
    items = sorted(comparison.items, key=lambda item: item.status == ComparisonItemStatus.MATCH)
    return CompareOutput(
        comparison_id=comparison.id,
        comparison_type=comparison.comparison_type.value,
        summary=dict(comparison.summary),
        items=[
            ComparedItem(
                item_key=item.item_key,
                check=item.check_name,
                line=item.line_key,
                status=item.status,
                left_value=clip(item.left_value, 200),
                right_value=clip(item.right_value, 200),
                difference=item.difference,
                explanation=clip(item.explanation, 300) or "",
            )
            for item in items[:MAX_COMPARISON_ITEMS]
        ],
        truncated=len(items) > MAX_COMPARISON_ITEMS,
    )


# ------------------------------------------------------------------------------ run_business_rules
class RulesInput(ToolInput):
    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=5)


class RuleOutcomeOut(ToolOutput):
    rule_code: str
    rule_name: str
    rule_type: str
    outcome: RuleOutcome
    severity: str
    message: str
    comparison_items: list[str]
    stored_outcome: RuleOutcome | None = Field(
        description="Outcome stored at the last evaluation (differs if data or rules changed)"
    )


class DuplicateOut(ToolOutput):
    document_id: uuid.UUID
    kind: str
    strong: bool


class RulesComparison(ToolOutput):
    comparison_type: str
    counterpart_ids: list[uuid.UUID]
    summary: dict[str, int]
    issues: list[ComparedItem] = Field(description="Items that are not MATCH")


class DocumentRules(ToolOutput):
    document_id: uuid.UUID
    evaluated: bool
    note: str | None
    results: list[RuleOutcomeOut]
    comparison: RulesComparison | None
    duplicates: list[DuplicateOut]


class RulesOutput(ToolOutput):
    documents: list[DocumentRules]


HIDDEN_DUPLICATE = "Possible duplicate of a document you cannot access."


async def _rules_for(scope: ToolScope, document_id: uuid.UUID) -> DocumentRules:
    document = await _visible_document(scope, document_id)
    if document.status not in PROCESSED:
        return DocumentRules(
            document_id=document.id,
            evaluated=False,
            note=f"Not processed yet (status {document.status.value}).",
            results=[],
            comparison=None,
            duplicates=[],
        )
    facts, assessment = await MatchingService(scope.session, scope.settings).dry_run(document)
    if facts is None:
        return DocumentRules(
            document_id=document.id,
            evaluated=False,
            note="No extracted fields to evaluate.",
            results=[],
            comparison=None,
            duplicates=[],
        )
    stored = {
        row.rule_code: row.outcome
        for row in await scope.session.scalars(
            select(RuleResultRecord).where(RuleResultRecord.document_id == document.id)
        )
    }
    # Matching looks across the document's department; show only what the reader may see.
    referenced: set[uuid.UUID] = {
        uuid.UUID(match.document_id) for match in assessment.duplicates if match.document_id
    }
    comparison = assessment.comparison
    partners: list[uuid.UUID] = []
    if comparison is not None:
        partners = [
            partner.document_id
            for partner in (comparison.purchase_order, *comparison.deliveries)
            if partner is not None and partner.document_id is not None
        ]
        referenced.update(partners)
    visible = await _visible_ids(scope, referenced)
    hidden_duplicates = any(
        match.document_id and uuid.UUID(match.document_id) not in visible
        for match in assessment.duplicates
    )
    results = []
    for result in assessment.results:
        message = result.message
        if result.rule.rule_type == "duplicate_document" and hidden_duplicates:
            message = HIDDEN_DUPLICATE
        results.append(
            RuleOutcomeOut(
                rule_code=result.rule.code,
                rule_name=result.rule.name,
                rule_type=result.rule.rule_type,
                outcome=result.outcome,
                severity=result.severity.value,
                message=clip(message, 500) or "",
                comparison_items=result.items[:10],
                stored_outcome=stored.get(result.rule.code),
            )
        )
    shown_comparison = None
    note = None
    if comparison is not None:
        if all(partner in visible for partner in partners):
            issues = [
                item for item in comparison.items if item.status != ComparisonItemStatus.MATCH
            ]
            shown_comparison = RulesComparison(
                comparison_type=comparison.comparison_type.value,
                counterpart_ids=partners,
                summary=comparison.summary,
                issues=[
                    ComparedItem(
                        item_key=item.key[:200],
                        check=item.check,
                        line=item.line,
                        status=ComparisonItemStatus(item.status.value),
                        left_value=clip(item.left_value, 200),
                        right_value=clip(item.right_value, 200),
                        difference=item.difference,
                        explanation=clip(item.explanation, 300) or "",
                    )
                    for item in issues[:30]
                ],
            )
        else:
            note = "The comparison involves documents you cannot access; it is not shown."
    return DocumentRules(
        document_id=document.id,
        evaluated=True,
        note=note,
        results=results,
        comparison=shown_comparison,
        duplicates=[
            DuplicateOut(
                document_id=uuid.UUID(match.document_id),
                kind=match.kind.value,
                strong=match.strong,
            )
            for match in assessment.duplicates
            if match.document_id and uuid.UUID(match.document_id) in visible
        ],
    )


async def run_business_rules(scope: ToolScope, args: RulesInput) -> RulesOutput:
    """Evaluate matching and every enabled rule on current data - nothing is stored."""
    return RulesOutput(
        documents=[
            await _rules_for(scope, document_id) for document_id in dict.fromkeys(args.document_ids)
        ]
    )


# ------------------------------------------------------------------------------ create_review_task
ReviewReasonText = Annotated[QueryText, StringConstraints(min_length=5, max_length=1000)]


class ReviewRequestInput(ToolInput):
    document_id: uuid.UUID
    reason: ReviewReasonText = Field(description="What a reviewer should check, and why")
    priority: ReviewPriority = ReviewPriority.NORMAL


class ReviewRequestOutput(ToolOutput):
    review_request_id: uuid.UUID
    created: bool = Field(description="False: the same request was already on file")
    task_id: uuid.UUID | None
    task_status: str | None
    task_priority: str | None
    document_status: DocumentStatus


async def create_review_task(scope: ToolScope, args: ReviewRequestInput) -> ReviewRequestOutput:
    outcome = await ReviewRequestService(scope.session, scope.settings).request(
        scope.actor,
        args.document_id,
        reason=args.reason,
        priority_level=args.priority,
        meta=scope.caller.meta,
        agent_run_id=scope.caller.run_id,
    )
    document = await scope.session.get(Document, args.document_id)
    task = outcome.task
    return ReviewRequestOutput(
        review_request_id=outcome.request.id,
        created=outcome.created,
        task_id=task.id if task else None,
        task_status=task.status.value if task else None,
        task_priority=task.priority.value if task else None,
        document_status=document.status if document else DocumentStatus.COMPLETED,
    )


# ------------------------------------------------------------------------------ the catalog
TOOLS: tuple[Tool[Any, Any], ...] = (
    Tool(
        name="search_documents",
        description="Find business documents by type, vendor, amounts, dates, payment terms "
        "or text. Returns ids with key facts; only documents the caller may see.",
        input_model=SearchDocumentsInput,
        output_model=SearchDocumentsOutput,
        permission=Permission.DOCUMENTS_READ,
        side_effect=SideEffect.READ,
        handler=search_documents,
        summarize=lambda out: {"total": out.total, "returned": len(out.results), "mode": out.mode},
    ),
    Tool(
        name="get_document",
        description="Metadata of one document: type and confidence, status, sensitivity, "
        "vendor, key facts, extraction quality and its open review task.",
        input_model=DocumentInput,
        output_model=DocumentInfo,
        permission=Permission.DOCUMENTS_READ,
        side_effect=SideEffect.READ,
        handler=get_document,
        summarize=lambda out: {
            "document_type": out.document_type,
            "status": out.status,
            "has_extraction": out.extraction is not None,
        },
    ),
    Tool(
        name="get_extracted_fields",
        description="Extracted field values of a document with confidence and evidence status; "
        "a reviewer's correction replaces the machine value.",
        input_model=ExtractedFieldsInput,
        output_model=ExtractedFieldsOutput,
        permission=Permission.DOCUMENTS_READ,
        side_effect=SideEffect.READ,
        handler=get_extracted_fields,
        summarize=lambda out: {
            "fields": len(out.fields),
            "missing": len(out.missing_paths),
            "review_level": out.review_level,
        },
    ),
    Tool(
        name="get_document_evidence",
        description="Where a field's value came from: page, verbatim source text, bounding "
        "box, evidence status and extraction method.",
        input_model=EvidenceInput,
        output_model=EvidenceOutput,
        permission=Permission.DOCUMENTS_READ,
        side_effect=SideEffect.READ,
        handler=get_document_evidence,
        summarize=lambda out: {"page": out.page_number, "evidence_status": out.evidence_status},
    ),
    Tool(
        name="search_knowledge_base",
        description="Retrieve policy, procedure and guideline passages in force on a date "
        "(hybrid search, access-controlled); no generated answer.",
        input_model=KnowledgeInput,
        output_model=KnowledgeOutput,
        permission=Permission.KNOWLEDGE_READ,
        side_effect=SideEffect.READ,
        handler=search_knowledge_base,
        summarize=lambda out: {
            "passages": len(out.passages),
            "sufficient_evidence": out.sufficient_evidence,
            "mode": out.mode,
        },
    ),
    Tool(
        name="compare_documents",
        description="Compare an invoice with a purchase order and/or delivery notes (or a "
        "delivery note with an order) field by field and line by line; the result is stored.",
        input_model=CompareInput,
        output_model=CompareOutput,
        permission=Permission.COMPARISONS_CREATE,
        side_effect=SideEffect.RECORD,
        handler=compare_documents,
        summarize=lambda out: {"comparison_id": str(out.comparison_id), "summary": out.summary},
    ),
    Tool(
        name="run_business_rules",
        description="Evaluate matching and every enabled business rule on the documents' "
        "current data (nothing is stored); returns outcomes, comparison issues and duplicates.",
        input_model=RulesInput,
        output_model=RulesOutput,
        permission=Permission.DOCUMENTS_READ,
        side_effect=SideEffect.READ,
        handler=run_business_rules,
        summarize=lambda out: {
            "documents": len(out.documents),
            "failing": sum(
                1
                for document in out.documents
                for result in document.results
                if result.outcome != RuleOutcome.PASS
                and result.outcome != RuleOutcome.NOT_APPLICABLE
            ),
        },
    ),
    Tool(
        name="create_review_task",
        description="Ask for a human review of a processed document: the request joins the "
        "document's open review task (or opens one) with the given reason and priority.",
        input_model=ReviewRequestInput,
        output_model=ReviewRequestOutput,
        permission=Permission.REVIEWS_WORK,
        side_effect=SideEffect.WRITE,
        handler=create_review_task,
        summarize=lambda out: {
            "created": out.created,
            "task_id": str(out.task_id) if out.task_id else None,
            "priority": out.task_priority,
        },
    ),
)

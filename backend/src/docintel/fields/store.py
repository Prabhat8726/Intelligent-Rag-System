"""Persistence of extraction results: rows <-> resolved fields, LLM output cache, corrections
carried over to a re-extraction of the same version."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.db.models import (
    DocumentExtraction,
    EvidenceStatusValue,
    ExtractedField,
    FieldOrigin,
    ReviewLevelValue,
)
from docintel.fields.candidates import Origin
from docintel.fields.evidence import EvidenceStatus
from docintel.fields.llm import PROMPT_VERSION
from docintel.fields.schemas import SchemaInfo, ValueType
from docintel.fields.service import (
    ExtractionOutcome,
    ExtractionPolicy,
    ResolvedField,
    Scoring,
    normalized_output_of,
    output_of,
    score_fields,
)


class DatabaseExtractionCache:
    """Reuse a stored model output for an identical model input (same prompt, schema, model)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def get(self, input_hash: str) -> dict[str, Any] | None:
        async with self._sessionmaker() as session:
            output: dict[str, Any] | None = await session.scalar(
                select(DocumentExtraction.llm_output)
                .where(
                    DocumentExtraction.input_hash == input_hash,
                    DocumentExtraction.llm_output.is_not(None),
                )
                .order_by(DocumentExtraction.created_at.desc())
                .limit(1)
            )
        return output


def _decimal(value: float) -> Decimal:
    return Decimal(f"{value:.4f}")


def field_row(position: int, item: ResolvedField) -> ExtractedField:
    return ExtractedField(
        position=position,
        field_path=item.path,
        field_name=item.name,
        group_name=item.group,
        row_index=item.row_index,
        value_type=item.value_type.value,
        is_required=item.required,
        original_value=item.original_value,
        normalized_value=item.normalized,
        page_number=item.page,
        source_text=item.source_text,
        bbox=item.bbox,
        evidence_status=EvidenceStatusValue(item.evidence.value),
        origin=FieldOrigin(item.origin.value) if item.origin else None,
        method=(item.method or "")[:300] or None,
        confidence=_decimal(item.confidence),
        confidence_signals=item.signals,
        alternatives=item.alternatives,
        corrected_value=item.corrected_value,
        corrected_normalized=item.corrected_normalized,
    )


def resolved_from_row(row: ExtractedField) -> ResolvedField:
    return ResolvedField(
        path=row.field_path,
        name=row.field_name,
        value_type=ValueType(row.value_type),
        required=row.is_required,
        original_value=row.original_value,
        normalized=row.normalized_value,
        page=row.page_number,
        source_text=row.source_text,
        bbox=row.bbox,
        evidence=EvidenceStatus(row.evidence_status.value),
        origin=Origin(row.origin.value) if row.origin else None,
        method=row.method,
        signals=dict(row.confidence_signals or {}),
        confidence=float(row.confidence),
        group=row.group_name,
        row_index=row.row_index,
        alternatives=list(row.alternatives or []),
        corrected_value=row.corrected_value,
        corrected_normalized=row.corrected_normalized,
    )


def apply_scoring(
    record: DocumentExtraction,
    schema: SchemaInfo,
    fields: list[ResolvedField],
    scoring: Scoring,
) -> None:
    """Write recomputed confidence, checks and routing back to a stored extraction."""
    by_path = {item.path: item for item in fields}
    for row in record.fields:
        item = by_path.get(row.field_path)
        if item is None:
            continue
        row.confidence = _decimal(item.confidence)
        row.confidence_signals = dict(item.signals)
    record.status = scoring.status
    record.overall_confidence = _decimal(scoring.confidence)
    record.review_level = ReviewLevelValue(scoring.level.value)
    record.checks = [check.to_json() for check in scoring.checks]
    record.output = output_of(schema, fields)
    record.normalized_output = normalized_output_of(schema, fields)
    record.signals = {
        **(record.signals or {}),
        "required_confidence": scoring.required_confidence,
        "line_items_confidence": scoring.line_items_confidence,
        "review_reasons": [reason.value for reason in scoring.reasons],
    }


def carry_over_corrections(
    previous: DocumentExtraction | None,
    outcome: ExtractionOutcome,
    policy: ExtractionPolicy,
) -> dict[str, ExtractedField]:
    """Re-apply human corrections from the previous extraction of the same version and schema.

    Returns the previous correction rows by path so their author and timestamp are kept.
    """
    if previous is None or previous.schema_name != outcome.schema.name:
        return {}
    corrected = {row.field_path: row for row in previous.fields if row.corrected_at is not None}
    if not corrected:
        return {}
    for item in outcome.fields:
        row = corrected.get(item.path)
        if row is None:
            continue
        item.corrected_value = row.corrected_value
        item.corrected_normalized = row.corrected_normalized
        item.signals["human"] = True
    outcome.scoring = score_fields(outcome.schema, outcome.fields, policy)
    return corrected


def extraction_record(
    *,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    outcome: ExtractionOutcome,
    corrections: dict[str, ExtractedField],
) -> DocumentExtraction:
    scoring = outcome.scoring
    llm = outcome.llm
    rows = []
    for position, item in enumerate(outcome.fields):
        row = field_row(position, item)
        previous = corrections.get(item.path)
        if previous is not None:
            row.corrected_by_id = previous.corrected_by_id
            row.corrected_at = previous.corrected_at
            row.correction_note = previous.correction_note
        rows.append(row)
    return DocumentExtraction(
        document_id=document_id,
        document_version_id=version_id,
        schema_name=outcome.schema.name,
        schema_version=outcome.schema.version,
        status=scoring.status,
        method=outcome.method,
        provider=llm.provider if llm and llm.output is not None else None,
        model=llm.model if llm and llm.output is not None else None,
        prompt_version=_prompt_version(outcome),
        input_hash=llm.input_hash if llm and llm.raw is not None else None,
        llm_output=llm.raw if llm else None,
        output=outcome.output(),
        normalized_output=outcome.normalized_output(),
        checks=[check.to_json() for check in scoring.checks],
        validation_errors=list(llm.errors) if llm else [],
        signals={
            **outcome.signals,
            "required_confidence": scoring.required_confidence,
            "line_items_confidence": scoring.line_items_confidence,
            "review_reasons": [reason.value for reason in scoring.reasons],
        },
        overall_confidence=_decimal(scoring.confidence),
        review_level=ReviewLevelValue(scoring.level.value),
        is_current=True,
        fields=rows,
    )


def _prompt_version(outcome: ExtractionOutcome) -> str | None:
    used = outcome.llm is not None and outcome.llm.output is not None
    return PROMPT_VERSION if used else None

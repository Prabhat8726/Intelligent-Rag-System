"""Structured extraction API schemas (Modules 6-8, 26)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, computed_field

from docintel.api.schemas.common import RequestModel, ResponseModel
from docintel.api.schemas.documents import UserSummary
from docintel.api.schemas.vendors import VendorSummary
from docintel.db.models import (
    EvidenceStatusValue,
    ExtractionMethodUsed,
    ExtractionStatus,
    FieldOrigin,
    ReviewLevelValue,
)


class ExtractedFieldRead(ResponseModel):
    id: uuid.UUID
    field_path: str = Field(description="e.g. 'total' or 'line_items[2].unit_price'")
    field_name: str
    group_name: str | None
    row_index: int | None
    value_type: str
    is_required: bool
    original_value: str | None = Field(description="As printed (or as the model transcribed it)")
    normalized_value: dict[str, Any] | None = Field(
        description="Typed value plus how it was decided (date order, currency source, ...)"
    )
    page_number: int | None
    source_text: str | None = Field(description="Verbatim quote the value was read from")
    bbox: list[float] | None = Field(description="[x0, y0, x1, y1] in the page's units")
    evidence_status: EvidenceStatusValue
    origin: FieldOrigin | None
    method: str | None = Field(description="How the value was found")
    confidence: Decimal
    confidence_signals: dict[str, Any]
    alternatives: list[dict[str, Any]] = Field(
        description="Value the other extractor proposed when they disagreed"
    )
    corrected_value: str | None
    corrected_normalized: dict[str, Any] | None
    correction_note: str | None
    corrected_by: UserSummary | None
    corrected_at: datetime | None


class CheckRead(ResponseModel):
    code: str
    status: str
    fields: list[str]
    expected: str
    actual: str
    message: str


class ExtractionRead(ResponseModel):
    id: uuid.UUID
    document_version_id: uuid.UUID
    schema_name: str
    schema_version: int
    status: ExtractionStatus
    method: ExtractionMethodUsed
    provider: str | None
    model: str | None
    prompt_version: str | None
    overall_confidence: Decimal
    review_level: ReviewLevelValue
    checks: list[CheckRead]
    signals: dict[str, Any] = Field(
        description="LLM use and gate decision, normalization context, vendor match"
    )
    validation_errors: list[Any] = Field(exclude=True, default_factory=list)
    created_at: datetime
    fields: list[ExtractedFieldRead]
    vendor: VendorSummary | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def validation_error_count(self) -> int:
        """Schema errors in the model output (repaired or not)."""
        return len(self.validation_errors)


class FieldEvidence(ResponseModel):
    id: uuid.UUID
    field_path: str
    value: str | None
    page_number: int | None
    source_text: str | None
    bbox: list[float] | None
    evidence_status: EvidenceStatusValue
    origin: FieldOrigin | None
    confidence: Decimal
    corrected: bool


class FieldCorrection(RequestModel):
    value: str = Field(
        max_length=500,
        description="Correct value as printed; empty = the value is not on the document",
    )
    note: str | None = Field(default=None, max_length=500)

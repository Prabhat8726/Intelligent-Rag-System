"""Structured extraction orchestration (Modules 6-8, 26).

    layout extractor (always) -> provisional score
      -> LLM extractor when allowed and needed (EXTRACTION_LLM_MODE, sensitivity gate, budget)
    -> merge field by field (agreement is a signal) -> evidence verification
    -> vendor resolution -> normalization -> consistency checks -> confidence -> routing

`score_fields` is shared with the API so human corrections re-run the same checks and routing.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Literal

from rapidfuzz import fuzz

from docintel.ai.base import LLMProvider
from docintel.ai.routing import ExternalAIGate, GateDecision
from docintel.core.config import Settings
from docintel.core.logging import get_logger
from docintel.db.models import (
    DocumentType,
    ExtractionMethodUsed,
    ExtractionStatus,
    ReviewReason,
    Sensitivity,
)
from docintel.fields.candidates import Candidate, ExtractorOutput, Origin, RowCandidate
from docintel.fields.confidence import (
    ReviewLevel,
    Thresholds,
    document_confidence,
    field_confidence,
    review_level,
)
from docintel.fields.evidence import Evidence, EvidenceLocator, EvidenceStatus
from docintel.fields.llm import ExtractionCache, LLMExtraction, LLMExtractor
from docintel.fields.local import LayoutExtractor
from docintel.fields.normalize import (
    DateOrder,
    NormalizationContext,
    NormalizationStatus,
    comparable,
    currencies_in,
    infer_date_order,
    name_similarity,
    normalize_value,
    organization_key,
    squash,
    value_in_source,
)
from docintel.fields.schemas import SchemaInfo, ValueType, schema_for
from docintel.fields.validation import (
    DEFAULT_TOLERANCE,
    Check,
    CheckStatus,
    consistency_by_field,
    run_checks,
)
from docintel.fields.vendors import StaticVendorDirectory, VendorDirectory, VendorMatch
from docintel.processing.content import DocumentTable, PageContent
from docintel.processing.sensitivity import TYPE_MINIMUM

logger = get_logger(__name__)

LLMMode = Literal["auto", "always", "never"]
_EVIDENCE_RANK = {
    EvidenceStatus.HUMAN: 4,
    EvidenceStatus.VERIFIED: 3,
    EvidenceStatus.FUZZY: 2,
    EvidenceStatus.UNSUPPORTED: 1,
    EvidenceStatus.NOT_FOUND: 0,
}
_VALUE_FOUND_SCORE = 90.0  # quote not found, but the value itself is printed on the page
# Only distinctive values may use that fallback: "0.00" or "10" occur anywhere on a page.
_VALUE_FOUND_MIN_CHARS = 6
_VENDOR_BACKED_SIMILARITY = 85.0
_ROW_MATCH_RATIO = 85.0
_TEXT_AGREEMENT = 95.0
CONFIRMED_LETTERHEAD_ANCHOR = 0.9


@dataclass(frozen=True, slots=True)
class ExtractionPolicy:
    llm_mode: LLMMode = "auto"
    thresholds: Thresholds = field(default_factory=Thresholds)
    tolerance: Decimal = DEFAULT_TOLERANCE
    vision_below_ocr_confidence: float = 70.0
    fuzzy_threshold: float = 85.0


def policy_from_settings(settings: Settings) -> ExtractionPolicy:
    return ExtractionPolicy(
        llm_mode=settings.extraction_llm_mode,
        thresholds=Thresholds(
            high=settings.extraction_confidence_high,
            medium=settings.extraction_confidence_medium,
        ),
        tolerance=settings.extraction_arithmetic_tolerance,
        vision_below_ocr_confidence=settings.extraction_vision_below_ocr_confidence,
        fuzzy_threshold=settings.evidence_fuzzy_threshold,
    )


def context_from_signals(signals: dict[str, Any]) -> NormalizationContext:
    """The document context an extraction was normalized with (for human corrections)."""
    stored = signals.get("context") or {}
    order = stored.get("date_order")
    return NormalizationContext(
        date_order=DateOrder(order) if order else None,
        date_order_reason=stored.get("date_order_reason"),
        currency=stored.get("currency"),
        decimal_comma=stored.get("decimal_comma"),
    )


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    document_type: DocumentType | None
    pages: Sequence[PageContent]
    tables: Sequence[DocumentTable]
    declared: Sensitivity = Sensitivity.INTERNAL
    detected: Sensitivity | None = None
    document_id: uuid.UUID | None = None
    classification_uncertain: bool = False


@dataclass(slots=True)
class ResolvedField:
    path: str
    name: str
    value_type: ValueType
    required: bool
    original_value: str | None
    normalized: dict[str, Any] | None  # Normalized.to_json()
    page: int | None
    source_text: str | None
    bbox: list[float] | None
    evidence: EvidenceStatus
    origin: Origin | None
    method: str | None
    signals: dict[str, Any]
    confidence: float = 0.0
    group: str | None = None
    row_index: int | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    # Human correction ("" = a reviewer confirmed the value is not on the document).
    corrected_value: str | None = None
    corrected_normalized: dict[str, Any] | None = None

    @property
    def corrected(self) -> bool:
        return self.corrected_value is not None

    @property
    def value(self) -> Any:
        source = self.corrected_normalized if self.corrected else self.normalized
        return None if source is None else source.get("value")

    @property
    def found(self) -> bool:
        return self.corrected or self.original_value is not None

    @property
    def display_value(self) -> str | None:
        return self.corrected_value if self.corrected else self.original_value


@dataclass(slots=True)
class Scoring:
    checks: list[Check]
    confidence: float
    required_confidence: float
    line_items_confidence: float | None
    level: ReviewLevel
    status: ExtractionStatus
    reasons: list[ReviewReason]


@dataclass(slots=True)
class ExtractionOutcome:
    schema: SchemaInfo
    method: ExtractionMethodUsed
    fields: list[ResolvedField]
    scoring: Scoring
    signals: dict[str, Any]
    llm: LLMExtraction | None = None
    vendor: VendorMatch | None = None

    @property
    def status(self) -> ExtractionStatus:
        return self.scoring.status

    def output(self) -> dict[str, Any]:
        return _assemble(self.schema, self.fields, lambda f: f.display_value)

    def normalized_output(self) -> dict[str, Any]:
        return _assemble(self.schema, self.fields, lambda f: f.value)


def _assemble(schema: SchemaInfo, fields: list[ResolvedField], pick: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    groups: dict[str, dict[int, dict[str, Any]]] = {}
    lists: dict[str, list[Any]] = {}
    table = schema.table.name if schema.table else None
    for item in fields:
        if item.group is None and item.name == table:
            continue  # the row count; the rows themselves follow
        if item.group is None:
            result[item.name] = pick(item)
        elif schema.table is not None and item.group == schema.table.name:
            groups.setdefault(item.group, {}).setdefault(item.row_index or 0, {})[item.name] = pick(
                item
            )
        else:
            lists.setdefault(item.group, []).append(pick(item))
    for name, rows in groups.items():
        result[name] = [rows[index] for index in sorted(rows)]
    result.update(lists)
    if schema.table is not None:
        result.setdefault(schema.table.name, [])
    for list_field in schema.lists:
        result.setdefault(list_field.name, [])
    return result


# ------------------------------------------------------------------------------ context
_DECIMAL_COMMA = re.compile(r"\d,\d{2}(?!\d)")
_DECIMAL_POINT = re.compile(r"\d\.\d{2}(?!\d)")


def infer_decimal_comma(texts: Sequence[str]) -> bool | None:
    commas = sum(len(_DECIMAL_COMMA.findall(text)) for text in texts)
    points = sum(len(_DECIMAL_POINT.findall(text)) for text in texts)
    if commas == points:
        return None
    return commas > points


def document_context(
    texts: Sequence[str], currency: str | None, decimal_comma: bool | None
) -> NormalizationContext:
    order, reason = infer_date_order(list(texts), currency)
    return NormalizationContext(
        date_order=order, date_order_reason=reason, currency=currency, decimal_comma=decimal_comma
    )


# ------------------------------------------------------------------------------ evidence
def _local_evidence(candidate: Candidate) -> Evidence:
    return Evidence(
        EvidenceStatus.VERIFIED,
        candidate.page,
        100.0,
        candidate.bbox,
        candidate.ocr_confidence,
        candidate.source_text,
    )


def _llm_evidence(
    candidate: Candidate,
    value_type: ValueType,
    locator: EvidenceLocator,
    context: NormalizationContext,
    cell: bool = False,
) -> Evidence:
    quote = candidate.source_text or candidate.raw_value
    evidence = locator.locate(quote, candidate.page)
    if evidence.status == EvidenceStatus.NOT_FOUND:
        if len(squash(candidate.raw_value)) < _VALUE_FOUND_MIN_CHARS:
            return evidence
        printed = locator.locate(candidate.raw_value, candidate.page)
        if printed.status == EvidenceStatus.VERIFIED:
            return replace(printed, status=EvidenceStatus.FUZZY, score=_VALUE_FOUND_SCORE)
        return printed if printed.status == EvidenceStatus.FUZZY else evidence
    if not value_in_source(value_type, candidate.raw_value, quote, context):
        return replace(evidence, status=EvidenceStatus.UNSUPPORTED)
    if cell:
        # Table cell: point the box at the cell inside the verified row when it is printed.
        located = locator.locate(candidate.raw_value, evidence.page, near=evidence.bbox)
        if located.status == EvidenceStatus.VERIFIED and located.page == evidence.page:
            return replace(evidence, bbox=located.bbox, ocr_confidence=located.ocr_confidence)
    return evidence


@dataclass(slots=True)
class _Assessed:
    candidate: Candidate
    evidence: Evidence
    normalized: dict[str, Any]
    normalization: NormalizationStatus
    key: Any


def _assess(
    candidate: Candidate,
    value_type: ValueType,
    locator: EvidenceLocator,
    context: NormalizationContext,
    cell: bool = False,
) -> _Assessed:
    normalized = normalize_value(value_type, candidate.raw_value, context)
    if candidate.origin in (Origin.LOCAL, Origin.DERIVED):
        evidence = _local_evidence(candidate)
    else:
        evidence = _llm_evidence(candidate, value_type, locator, context, cell)
    return _Assessed(
        candidate,
        evidence,
        normalized.to_json(),
        normalized.status,
        comparable(value_type, normalized.value) if normalized.valid else None,
    )


def _agree(value_type: ValueType, a: _Assessed, b: _Assessed) -> bool:
    if a.key is not None and a.key == b.key:
        return True
    if value_type in (ValueType.ORGANIZATION, ValueType.PERSON, ValueType.TEXT):
        return name_similarity(str(a.key or ""), str(b.key or "")) >= _TEXT_AGREEMENT
    return False


def _pick(local: _Assessed, model: _Assessed) -> _Assessed:
    """Which of two disagreeing values to show first (both stay visible to the reviewer)."""
    local_rank = _EVIDENCE_RANK[local.evidence.status]
    model_rank = _EVIDENCE_RANK[model.evidence.status]
    if local_rank != model_rank:
        return local if local_rank > model_rank else model
    return local if local.candidate.anchor >= 0.95 else model


def _resolved(
    path: str,
    name: str,
    value_type: ValueType,
    required: bool,
    chosen: _Assessed | None,
    *,
    agreement: bool | None,
    origin: Origin | None = None,
    other: _Assessed | None = None,
    group: str | None = None,
    row_index: int | None = None,
) -> ResolvedField:
    if chosen is None:
        return ResolvedField(
            path,
            name,
            value_type,
            required,
            None,
            None,
            None,
            None,
            None,
            EvidenceStatus.NOT_FOUND,
            None,
            None,
            {"missing": True},
            0.0,
            group,
            row_index,
        )
    evidence = chosen.evidence
    signals: dict[str, Any] = {
        "evidence": evidence.status.value,
        "evidence_score": evidence.score,
        "normalization": chosen.normalization.value,
        "ocr": evidence.ocr_confidence,
        "anchor": round(chosen.candidate.anchor, 4),
        "conflicts": chosen.candidate.conflicts,
        "page_matches_citation": evidence.page_matches_citation,
        "agreement": agreement,
        "consistency": None,
    }
    alternatives = []
    if other is not None and agreement is False:
        alternatives.append(
            {
                "origin": other.candidate.origin.value,
                "value": other.candidate.raw_value,
                "page": other.candidate.page,
                "evidence": other.evidence.status.value,
            }
        )
    return ResolvedField(
        path=path,
        name=name,
        value_type=value_type,
        required=required,
        original_value=chosen.candidate.raw_value,
        normalized=chosen.normalized,
        page=evidence.page or chosen.candidate.page,
        source_text=chosen.candidate.source_text,
        bbox=evidence.bbox.to_list() if evidence.bbox else None,
        evidence=evidence.status,
        origin=origin or chosen.candidate.origin,
        method=chosen.candidate.method,
        signals=signals,
        group=group,
        row_index=row_index,
        alternatives=alternatives,
    )


def _merge(
    path: str,
    name: str,
    value_type: ValueType,
    required: bool,
    local: _Assessed | None,
    model: _Assessed | None,
    *,
    group: str | None = None,
    row_index: int | None = None,
    confirmed: _Assessed | None = None,
) -> ResolvedField:
    """`confirmed`: the candidate independent master data backs (shown first on disagreement)."""
    if local is not None and model is not None:
        if _agree(value_type, local, model):
            chosen = (
                local
                if _EVIDENCE_RANK[local.evidence.status] >= _EVIDENCE_RANK[model.evidence.status]
                else model
            )
            return _resolved(
                path, name, value_type, required, chosen, agreement=True, origin=Origin.BOTH,
                group=group, row_index=row_index,
            )  # fmt: skip
        chosen = confirmed if confirmed in (local, model) else _pick(local, model)
        other = model if chosen is local else local
        return _resolved(
            path, name, value_type, required, chosen, agreement=False, other=other,
            group=group, row_index=row_index,
        )  # fmt: skip
    return _resolved(
        path, name, value_type, required, local or model, agreement=None,
        group=group, row_index=row_index,
    )  # fmt: skip


def _vendor_backed(vendor: VendorMatch | None, *candidates: _Assessed | None) -> _Assessed | None:
    """The vendor-name candidate the vendor master recognizes (a name a document's text talks
    the model into is unlikely to be a known vendor)."""
    if vendor is None:
        return None
    canonical = organization_key(vendor.canonical_name)
    for candidate in candidates:
        if (
            candidate is not None
            and name_similarity(organization_key(candidate.candidate.raw_value), canonical)
            >= _VENDOR_BACKED_SIMILARITY
        ):
            return candidate
    return None


def _row_count_field(name: str, required: bool, fields: list[ResolvedField]) -> ResolvedField:
    """How many table rows were read, as a field of its own: required for line-item documents,
    so a document whose table was not found is never auto-accepted. A reviewer confirms "none"
    (empty correction) when the document really has no rows."""
    rows = sorted({f.row_index for f in fields if f.group == name and f.row_index is not None})
    if not rows:
        missing = _resolved(name, name, ValueType.INTEGER, required, None, agreement=None)
        missing.method = "no table rows found"
        return missing
    first_page = next((f.page for f in fields if f.group == name and f.page is not None), None)
    return ResolvedField(
        path=name,
        name=name,
        value_type=ValueType.INTEGER,
        required=required,
        original_value=str(len(rows)),
        normalized={"value": len(rows), "status": NormalizationStatus.OK.value},
        page=first_page,
        source_text=None,
        bbox=None,
        evidence=EvidenceStatus.VERIFIED,
        origin=Origin.DERIVED,
        method="rows of the detected table",
        signals={
            "evidence": EvidenceStatus.VERIFIED.value,
            "evidence_score": 100.0,
            "normalization": NormalizationStatus.OK.value,
            "ocr": None,
            "anchor": 1.0,
            "conflicts": 0,
            "page_matches_citation": True,
            "agreement": None,
            "consistency": None,
        },
    )


def _row_key(row: RowCandidate) -> str:
    sku = row.cells.get("sku")
    if sku is not None:
        return "sku:" + squash(sku.raw_value)
    description = row.cells.get("description")
    return "desc:" + squash(description.raw_value) if description else ""


def pair_rows(
    local_rows: Sequence[RowCandidate], model_rows: Sequence[RowCandidate]
) -> list[tuple[RowCandidate | None, RowCandidate | None]]:
    """Pair rows from the two extractors by SKU, then description similarity, then position."""
    remaining = list(model_rows)
    pairs: list[tuple[RowCandidate | None, RowCandidate | None]] = []
    for row in local_rows:
        key = _row_key(row)
        match = next((other for other in remaining if key and _row_key(other) == key), None)
        if match is None and "description" in row.cells:
            wanted = squash(row.cells["description"].raw_value)
            scored = [
                (fuzz.ratio(wanted, squash(other.cells["description"].raw_value)), other)
                for other in remaining
                if "description" in other.cells
            ]
            best = max(scored, key=lambda item: item[0], default=None)
            if best is not None and best[0] >= _ROW_MATCH_RATIO:
                match = best[1]
        if match is not None:
            remaining.remove(match)
        pairs.append((row, match))
    if len(local_rows) == len(model_rows) and all(m is None for _, m in pairs):
        return list(zip(local_rows, model_rows, strict=True))  # same table, keys unreadable
    pairs.extend((None, row) for row in remaining)
    return pairs


# ------------------------------------------------------------------------------ scoring
def apply_correction(
    item: ResolvedField, value: str, context: NormalizationContext | None = None
) -> bool:
    """Record a reviewer's value. Returns False if it does not parse as the field's type."""
    text = value.strip()
    if text:
        normalized = normalize_value(item.value_type, text, context)
        if not normalized.valid:
            return False
        item.corrected_normalized = normalized.to_json()
    else:
        item.corrected_normalized = {"value": None, "status": "CONFIRMED_EMPTY"}
    item.corrected_value = text
    item.signals["human"] = True
    return True


def output_of(schema: SchemaInfo, fields: list[ResolvedField]) -> dict[str, Any]:
    return _assemble(schema, fields, lambda f: f.display_value)


def normalized_output_of(schema: SchemaInfo, fields: list[ResolvedField]) -> dict[str, Any]:
    return _assemble(schema, fields, lambda f: f.value)


def score_fields(
    schema: SchemaInfo, fields: list[ResolvedField], policy: ExtractionPolicy
) -> Scoring:
    """Consistency checks, field and document confidence, routing - recomputable at any time."""
    scalars = {item.name: item.value for item in fields if item.group is None}
    table = schema.table.name if schema.table else None
    rows: dict[int, dict[str, Any]] = {}
    for item in fields:
        if table is not None and item.group == table and item.row_index is not None:
            rows.setdefault(item.row_index, {})[item.name] = item.value
    ordered_rows = [rows[index] for index in sorted(rows)]
    checks = run_checks(scalars, ordered_rows, table=table, tolerance=policy.tolerance)
    consistency = consistency_by_field(checks)
    for item in fields:
        if not item.found:
            item.confidence = 0.0
            continue
        item.signals["consistency"] = consistency.get(item.path)
        item.confidence = field_confidence(item.signals)
    required = [
        item.confidence if item.found else None
        for item in fields
        if item.group is None and item.required
    ]
    required_confidence = document_confidence(required)
    line_cells = [
        item.confidence
        for item in fields
        if table
        and item.group == table
        and (column := schema.column(item.name)) is not None
        and column.meta.routing
    ]
    line_confidence = round(min(line_cells), 4) if line_cells else None
    overall = min(required_confidence, line_confidence if line_confidence is not None else 1.0)
    failed = any(check.status == CheckStatus.FAIL for check in checks)
    level = review_level(overall, failed_checks=failed, thresholds=policy.thresholds)

    missing = [
        item.name for item in fields if item.group is None and item.required and not item.found
    ]
    anything = any(item.found for item in fields)
    if not anything:
        status = ExtractionStatus.FAILED
    elif missing:
        status = ExtractionStatus.PARTIAL
    else:
        status = ExtractionStatus.SUCCEEDED
    reasons: list[ReviewReason] = []
    if status == ExtractionStatus.FAILED:
        reasons.append(ReviewReason.EXTRACTION_FAILED)
    elif missing:
        reasons.append(ReviewReason.MISSING_REQUIRED_FIELDS)
    if failed:
        reasons.append(ReviewReason.EXTRACTION_INCONSISTENT)
    if level != ReviewLevel.AUTO and not reasons:
        reasons.append(ReviewReason.EXTRACTION_UNCERTAIN)
    return Scoring(checks, overall, required_confidence, line_confidence, level, status, reasons)


# ------------------------------------------------------------------------------ service
class FieldExtractionService:
    def __init__(
        self,
        *,
        policy: ExtractionPolicy | None = None,
        llm: LLMProvider | None = None,
        gate: ExternalAIGate | None = None,
        vendors: VendorDirectory | None = None,
        cache: ExtractionCache | None = None,
        max_prompt_chars: int = 60_000,
        max_output_tokens: int = 8192,
        max_images: int = 2,
    ) -> None:
        self._policy = policy or ExtractionPolicy()
        self._llm = (
            LLMExtractor(
                llm,
                cache=cache,
                max_prompt_chars=max_prompt_chars,
                max_output_tokens=max_output_tokens,
                max_images=max_images,
            )
            if llm is not None and self._policy.llm_mode != "never"
            else None
        )
        self._gate = gate or ExternalAIGate(
            max_sensitivity=Sensitivity.INTERNAL,
            provider_configured=llm is not None,
            provider_local=llm.local if llm is not None else False,
        )
        self._vendors = vendors or StaticVendorDirectory()

    @property
    def policy(self) -> ExtractionPolicy:
        return self._policy

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome | None:
        schema = schema_for(request.document_type)
        if schema is None:
            return None
        texts = [page.text for page in request.pages]
        decimal_comma = infer_decimal_comma(texts)
        first_context = document_context(texts, None, decimal_comma)
        local = LayoutExtractor(schema).extract(request.pages, request.tables, first_context)
        locator = EvidenceLocator(request.pages, self._policy.fuzzy_threshold)

        outcome = await self._resolve(schema, request, local, None, locator, decimal_comma)
        llm_signal: dict[str, Any] = {"mode": self._policy.llm_mode, "used": False}
        decision = self._decide(request, schema)
        outcome.signals["external_ai"] = decision.to_json()
        if self._llm is None:
            llm_signal["reason"] = (
                "disabled (EXTRACTION_LLM_MODE=never)"
                if self._policy.llm_mode == "never"
                else "no LLM provider configured"
            )
        elif not decision.allowed:
            llm_signal["reason"] = decision.reason
        elif (
            self._policy.llm_mode == "auto"
            and outcome.scoring.level == ReviewLevel.AUTO
            and outcome.status == ExtractionStatus.SUCCEEDED
        ):
            llm_signal["reason"] = "layout extraction was confident"
        else:
            vision = [
                page.page_number
                for page in request.pages
                if page.ocr_confidence is not None
                and page.ocr_confidence < self._policy.vision_below_ocr_confidence
            ]
            extraction = await self._llm.extract(
                schema, request.pages, document_id=request.document_id, vision_pages=vision
            )
            llm_signal.update(
                {
                    "used": extraction.output is not None,
                    "provider": extraction.provider,
                    "model": extraction.model,
                    "cache_hit": extraction.cache_hit,
                    "repaired": extraction.repaired,
                    "truncated": extraction.truncated,
                    "image_pages": list(extraction.image_pages),
                    "errors": extraction.errors[:10],
                }
            )
            if extraction.output is None:
                llm_signal["reason"] = "model output unusable; layout result kept"
            else:
                outcome = await self._resolve(
                    schema, request, local, extraction, locator, decimal_comma
                )
                outcome.signals["external_ai"] = decision.to_json()
            outcome.llm = extraction
        outcome.signals["llm"] = llm_signal
        return outcome

    def _decide(self, request: ExtractionRequest, schema: SchemaInfo) -> GateDecision:
        return self._gate.decide(
            request.declared, request.detected, TYPE_MINIMUM.get(schema.document_type)
        )

    async def _resolve(
        self,
        schema: SchemaInfo,
        request: ExtractionRequest,
        local: ExtractorOutput,
        extraction: LLMExtraction | None,
        locator: EvidenceLocator,
        decimal_comma: bool | None,
    ) -> ExtractionOutcome:
        model = extraction.output if extraction is not None else None
        texts = [page.text for page in request.pages]

        # Vendor first: its master data can supply the document currency.
        vendor = await self._match_vendor(schema, local, model)
        currency = self._currency(schema, local, model, vendor, texts)
        context = document_context(texts, currency, decimal_comma)

        fields: list[ResolvedField] = []
        for scalar in schema.scalars:
            local_item = local.scalars.get(scalar.name)
            model_item = model.scalars.get(scalar.name) if model else None
            assessed_local = (
                _assess(local_item, scalar.meta.type, locator, context) if local_item else None
            )
            assessed_model = (
                _assess(model_item, scalar.meta.type, locator, context) if model_item else None
            )
            confirmed = (
                _vendor_backed(vendor, assessed_local, assessed_model)
                if scalar.meta.vendor
                else None
            )
            resolved = _merge(
                scalar.name,
                scalar.name,
                scalar.meta.type,
                scalar.meta.required,
                assessed_local,
                assessed_model,
                confirmed=confirmed,
            )
            self._apply_vendor(scalar.meta.vendor, scalar.name, resolved, vendor)
            fields.append(resolved)

        if schema.table is not None:
            column_types = {column.name: column.meta for column in schema.table.columns}
            pairs = pair_rows(local.rows, model.rows if model else [])
            for index, (local_row, model_row) in enumerate(pairs):
                for column in schema.table.columns:
                    local_cell = local_row.cells.get(column.name) if local_row else None
                    model_cell = model_row.cells.get(column.name) if model_row else None
                    if local_cell is None and model_cell is None:
                        continue
                    meta = column_types[column.name]
                    fields.append(
                        _merge(
                            f"{schema.table.name}[{index}].{column.name}",
                            column.name,
                            meta.type,
                            meta.required,
                            _assess(local_cell, meta.type, locator, context)
                            if local_cell
                            else None,
                            _assess(model_cell, meta.type, locator, context, cell=True)
                            if model_cell
                            else None,
                            group=schema.table.name,
                            row_index=index,
                        )
                    )

        if schema.table is not None:
            count = _row_count_field(schema.table.name, schema.rows_required, fields)
            fields.insert(len(schema.scalars), count)

        for list_field in schema.lists:
            local_items = local.lists.get(list_field.name, [])
            model_items = model.lists.get(list_field.name, []) if model else []
            seen: dict[str, Candidate] = {}
            agreed: set[str] = set()
            for item in [*local_items, *model_items]:
                key = squash(item.raw_value)
                if key in seen and seen[key].origin != item.origin:
                    agreed.add(key)
                seen.setdefault(key, item)
            for position, (key, item) in enumerate(seen.items()):
                assessed = _assess(item, list_field.meta.type, locator, context)
                both = key in agreed
                fields.append(
                    _resolved(
                        f"{list_field.name}[{position}]",
                        list_field.name,
                        list_field.meta.type,
                        False,
                        assessed,
                        agreement=True if both else None,
                        origin=Origin.BOTH if both else None,
                        group=list_field.name,
                        row_index=position,
                    )
                )

        scoring = score_fields(schema, fields, self._policy)
        if local.empty and model is not None and not model.empty:
            method = ExtractionMethodUsed.LLM
        elif model is not None:
            method = ExtractionMethodUsed.COMBINED
        else:
            method = ExtractionMethodUsed.LOCAL
        signals: dict[str, Any] = {
            "context": {
                "date_order": context.date_order.value if context.date_order else None,
                "date_order_reason": context.date_order_reason,
                "currency": context.currency,
                "decimal_comma": context.decimal_comma,
            },
            "vendor": vendor.to_json() if vendor else None,
            "classification_uncertain": request.classification_uncertain,
            "required_confidence": scoring.required_confidence,
            "line_items_confidence": scoring.line_items_confidence,
        }
        return ExtractionOutcome(schema, method, fields, scoring, signals, extraction, vendor)

    async def _match_vendor(
        self, schema: SchemaInfo, local: ExtractorOutput, model: ExtractorOutput | None
    ) -> VendorMatch | None:
        vendor_field = next((f for f in schema.scalars if f.meta.vendor), None)
        if vendor_field is None:
            return None
        names: list[str | None] = [
            source.scalars[vendor_field.name].raw_value
            for source in (local, model)
            if source is not None and vendor_field.name in source.scalars
        ]
        tax_ids: list[str | None] = [
            source.scalars["vendor_tax_id"].raw_value
            for source in (local, model)
            if source is not None and "vendor_tax_id" in source.scalars
        ]
        for name in names or [None]:
            for tax_id in tax_ids or [None]:
                match = await self._vendors.match(name, tax_id)
                if match is not None:
                    return match
        return None

    def _currency(
        self,
        schema: SchemaInfo,
        local: ExtractorOutput,
        model: ExtractorOutput | None,
        vendor: VendorMatch | None,
        texts: Sequence[str] = (),
    ) -> str | None:
        for source in (local, model):
            if source is None or "currency" not in source.scalars:
                continue
            candidate = source.scalars["currency"]
            if candidate.origin == Origin.DERIVED and candidate.anchor < 0.9:
                continue  # an assumed "$" must not beat the vendor's known currency
            normalized = normalize_value(ValueType.CURRENCY, candidate.raw_value)
            if normalized.valid:
                return str(normalized.value)
        if vendor is not None and vendor.default_currency:
            return vendor.default_currency
        printed = set().union(*(currencies_in(text) for text in texts)) if texts else set()
        if len(printed) == 1:
            return printed.pop()  # one currency code/symbol printed on the whole document
        assumed = local.scalars.get("currency")
        if assumed is not None:
            normalized = normalize_value(ValueType.CURRENCY, assumed.raw_value)
            if normalized.valid:
                return str(normalized.value)
        return None

    @staticmethod
    def _apply_vendor(
        is_vendor_field: bool, name: str, resolved: ResolvedField, vendor: VendorMatch | None
    ) -> None:
        if not resolved.found or resolved.normalized is None:
            return
        if is_vendor_field:
            resolved.normalized["vendor"] = vendor.to_json() if vendor else None
        relevant = is_vendor_field or (
            name == "vendor_tax_id" and vendor is not None and vendor.method == "tax_id"
        )
        if vendor is None or not relevant:
            return
        if resolved.signals.get("agreement") is None:
            resolved.signals["agreement"] = True
            resolved.signals["agreement_source"] = "vendor master"
        if is_vendor_field and resolved.signals.get("anchor", 1.0) < CONFIRMED_LETTERHEAD_ANCHOR:
            # A letterhead name that is a known vendor answers "is this the vendor?".
            resolved.signals["anchor"] = CONFIRMED_LETTERHEAD_ANCHOR
            resolved.signals["anchor_note"] = "letterhead name confirmed by the vendor master"

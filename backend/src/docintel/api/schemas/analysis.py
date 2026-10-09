"""Agent analysis API schemas (Modules 14, 15, 19 "AI Analysis")."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import Field

from docintel.agent.state import (
    ActionRecord,
    Confidence,
    EvidenceItem,
    Finding,
    Intent,
    Plan,
    Recommendation,
)
from docintel.api.schemas.common import QueryText, RequestModel, ResponseModel
from docintel.db.models import AgentRunStatus, ToolCallStatus


class AnalysisCreate(RequestModel):
    query: QueryText = Field(
        min_length=3,
        max_length=1000,
        description="What to investigate, e.g. 'Can we pay this invoice?'",
    )
    document_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=5, description="Documents to investigate (optional)"
    )
    allow_safe_actions: bool = Field(
        default=True,
        description="Let the investigation request a human review itself (low-risk action); "
        "actions that need approval are only ever proposed",
    )


class AnalysisDocument(ResponseModel):
    label: str | None
    role: str = Field(description="'subject' (investigated) or 'related' (order, delivery...)")
    document_id: uuid.UUID
    filename: str
    document_type: str | None
    status: str
    vendor_name: str | None
    document_date: date | None
    total: str | None
    currency: str | None
    effective_sensitivity: str | None


class AnalysisSource(ResponseModel):
    label: str | None
    sent_to_model: bool
    chunk_id: uuid.UUID
    knowledge_document_id: uuid.UUID
    title: str
    version_label: str | None
    section_path: str
    page_start: int | None
    page_end: int | None
    effective_from: date | None
    effective_to: date | None
    content: str
    query: str | None


class AnalysisComparison(ResponseModel):
    comparison_id: uuid.UUID
    comparison_type: str
    summary: dict[str, int]


class AnalysisResult(ResponseModel):
    summary: str
    summary_source: str = Field(description="'rules' (deterministic) or 'model' (validated)")
    intent: Intent
    documents: list[AnalysisDocument]
    findings: list[Finding]
    evidence: list[EvidenceItem]
    sources: list[AnalysisSource]
    comparisons: list[AnalysisComparison]
    confidence: Confidence
    recommendation: Recommendation
    action: ActionRecord | None
    notices: list[str]
    model: dict[str, str] | None


class ToolCallRead(ResponseModel):
    id: uuid.UUID
    node_name: str | None
    tool_name: str
    status: ToolCallStatus
    error: str | None
    latency_ms: Decimal
    arguments: dict[str, Any]
    result_summary: dict[str, Any] | None
    created_at: datetime


class TraceStep(ResponseModel):
    node: str
    duration_ms: float
    tool_calls: int


class AnalysisSummary(ResponseModel):
    id: uuid.UUID
    status: AgentRunStatus
    query: str
    document_ids: list[uuid.UUID]
    intent: Intent | None = None
    recommendation: str | None = None
    confidence: str | None = None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AnalysisUsage(ResponseModel):
    tool_calls: int
    llm_calls: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: Decimal | None


class AnalysisRead(AnalysisSummary):
    graph_version: str
    allow_safe_actions: bool
    plan: Plan | None
    result: AnalysisResult | None
    trace: list[TraceStep]
    tool_call_log: list[ToolCallRead]
    usage: AnalysisUsage


class AnalysisPage(ResponseModel):
    items: list[AnalysisSummary]
    total: int
    limit: int
    offset: int

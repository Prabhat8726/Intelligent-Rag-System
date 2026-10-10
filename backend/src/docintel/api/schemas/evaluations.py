"""Recorded evaluation runs (Phase 10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from docintel.api.schemas.common import ResponseModel


class EvaluationHeadline(ResponseModel):
    label: str
    value: str | None = Field(description="None when this report lacks the metric (quick run)")


class GateSummary(ResponseModel):
    passed: bool
    mode: str = Field(description="full or quick: the gates that apply to this kind of run")
    checks: int
    failed: int


class GateCheck(ResponseModel):
    metric: list[str]
    value: float | int | None
    min: float | None
    max: float | None
    passed: bool
    problem: str | None
    why: str


class ReportTable(ResponseModel):
    heading: str
    header: list[str]
    rows: list[list[str]]


class EvaluationSummary(ResponseModel):
    id: uuid.UUID
    suite: str
    title: str
    quick: bool
    git_revision: str | None
    run_at: datetime
    recorded_at: datetime
    source: str = Field(description="RUN (recorded by the run) or IMPORT (from report files)")
    recorded_by: str | None = Field(description="Full name of the person who recorded it")
    gates: GateSummary | None = Field(description="None when no gate applied when recorded")
    headlines: list[EvaluationHeadline]


class EvaluationPage(ResponseModel):
    items: list[EvaluationSummary]
    total: int
    limit: int
    offset: int


class EvaluationDetail(EvaluationSummary):
    dataset: dict[str, Any]
    config: dict[str, Any]
    environment: dict[str, Any]
    metrics: dict[str, Any]
    notes: list[str]
    tables: list[ReportTable]
    gate_checks: list[GateCheck]
    report_markdown: str

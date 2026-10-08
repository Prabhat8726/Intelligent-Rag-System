"""Health/readiness schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class CheckResult(BaseModel):
    status: Literal["ok", "fail"]
    detail: str | None = None
    latency_ms: float | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    version: str
    checks: dict[str, CheckResult]

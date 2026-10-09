"""Shared API schema building blocks."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from docintel.core.text import QueryText


class RequestModel(BaseModel):
    """Base for request bodies: unknown fields are rejected.

    Strings are NOT stripped globally (that would silently alter passwords); fields that need
    normalization do it explicitly.
    """

    model_config = ConfigDict(extra="forbid")


class ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProblemDetail(BaseModel):
    """RFC 9457 error body (documented in OpenAPI for every error response)."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    instance: str | None = None
    request_id: str | None = None
    errors: list[dict[str, str]] | None = None


PROBLEM_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ProblemDetail, "description": "Missing, invalid or expired credentials"},
    403: {"model": ProblemDetail, "description": "Authenticated but not permitted"},
    422: {"model": ProblemDetail, "description": "Request validation failed"},
}


__all__ = ["PROBLEM_RESPONSES", "ProblemDetail", "QueryText", "RequestModel", "ResponseModel"]

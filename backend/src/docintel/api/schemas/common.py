"""Shared API schema building blocks."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict


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


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _plain_text(value: str) -> str:
    """Search and question text: no control characters (PostgreSQL text cannot hold NUL)."""
    if _CONTROL.search(value):
        msg = "must not contain control characters"
        raise ValueError(msg)
    if not value.strip():
        msg = "must not be blank"
        raise ValueError(msg)
    return value.strip()


QueryText = Annotated[str, AfterValidator(_plain_text)]

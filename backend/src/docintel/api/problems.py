"""RFC 9457 problem-details responses and exception handlers.

Responses never contain stack traces, SQL, or the raw input that failed validation (a failed
login body would otherwise echo the password back).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from docintel.core.context import current_request_id
from docintel.core.errors import AppError
from docintel.core.logging import get_logger

PROBLEM_CONTENT_TYPE = "application/problem+json"

logger = get_logger(__name__)


def problem_response(
    *,
    status: int,
    detail: str,
    instance: str | None,
    title: str | None = None,
    headers: Mapping[str, str] | None = None,
    errors: Sequence[Mapping[str, Any]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title or HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "instance": instance,
        "request_id": current_request_id(),
    }
    if errors:
        body["errors"] = list(errors)
    return JSONResponse(
        body, status_code=status, headers=dict(headers or {}), media_type=PROBLEM_CONTENT_TYPE
    )


def _sanitize_validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Keep location/message/type only - drop `input` and `ctx`, which can echo secrets."""
    sanitized: list[dict[str, Any]] = []
    for error in errors:
        location = [str(part) for part in error.get("loc", ())]
        sanitized.append(
            {
                "field": ".".join(location),
                "message": str(error.get("msg", "Invalid value")),
                "type": str(error.get("type", "value_error")),
            }
        )
    return sanitized


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):  # registration guarantees the type
        raise exc
    return problem_response(
        status=exc.status_code,
        title=exc.title,
        detail=exc.detail,
        instance=request.url.path,
        headers=exc.headers,
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # registration guarantees the type
        raise exc
    detail = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    return problem_response(
        status=exc.status_code,
        detail=detail,
        instance=request.url.path,
        headers=exc.headers,
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # registration guarantees the type
        raise exc
    return problem_response(
        status=422,
        detail="The request is invalid.",
        instance=request.url.path,
        errors=_sanitize_validation_errors(exc.errors()),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)

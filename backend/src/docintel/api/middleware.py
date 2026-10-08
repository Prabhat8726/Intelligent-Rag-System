"""Pure-ASGI middleware (no BaseHTTPMiddleware: keeps contextvars and streaming intact).

Order in the app (outermost first): SecurityHeaders → RequestContext → routing.
RequestContext also converts unhandled exceptions into a problem response so even 500s carry
the request id and security headers.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Iterable

import structlog
from fastapi import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from docintel.api.problems import PROBLEM_CONTENT_TYPE
from docintel.core.context import current_request_id, request_id_var
from docintel.core.logging import get_logger

logger = get_logger("docintel.http")

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_QUIET_PATHS = frozenset({"/health", "/health/ready"})


def _resolve_request_id(headers: Headers) -> str:
    candidate = headers.get(REQUEST_ID_HEADER)
    if candidate and _VALID_REQUEST_ID.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(Headers(scope=scope))
        token = request_id_var.set(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception("http.unhandled_exception", path=scope.get("path"))
            if not response_started:
                await _send_internal_error(send_with_request_id, scope, request_id)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            path = scope.get("path", "")
            log = logger.debug if path in _QUIET_PATHS else logger.info
            log(
                "http.request",
                method=scope.get("method"),
                path=path,  # path only: query strings may carry sensitive values
                status=status_code,
                duration_ms=duration_ms,
                client_ip=(scope.get("client") or (None,))[0],
            )
            structlog.contextvars.unbind_contextvars("request_id")
            request_id_var.reset(token)


async def _send_internal_error(send: Send, scope: Scope, request_id: str) -> None:
    await _send_problem(
        send, scope, 500, "Internal Server Error", "An unexpected error occurred.", request_id
    )


class SecurityHeadersMiddleware:
    """Adds defensive headers to every HTTP response."""

    # Swagger UI / ReDoc load assets from a CDN; they get no CSP here (disabled in production).
    _DOCS_PATHS = ("/docs", "/redoc")

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    def _headers_for(self, path: str) -> Iterable[tuple[str, str]]:
        yield "X-Content-Type-Options", "nosniff"
        yield "X-Frame-Options", "DENY"
        yield "Referrer-Policy", "no-referrer"
        yield "Cross-Origin-Opener-Policy", "same-origin"
        yield "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        if not path.startswith(self._DOCS_PATHS):
            yield "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
        if path.startswith("/api/"):
            yield "Cache-Control", "no-store"
        if self.hsts:
            yield "Strict-Transport-Security", "max-age=63072000; includeSubDomains"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self._headers_for(path):
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """Caps request body size while the body streams in (before any parsing or spooling).

    Starlette parses multipart bodies completely before the endpoint runs, so the limit must be
    enforced here: a declared Content-Length over the limit is rejected immediately, and bodies
    without one (chunked) are counted and cut off as soon as they exceed it.
    """

    _METHODS = frozenset({"POST", "PUT", "PATCH"})

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int,
        overrides: dict[tuple[str, str], int] | None = None,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.overrides = dict(overrides or {})

    def _limit_for(self, method: str, path: str) -> int:
        return self.overrides.get((method, path.rstrip("/") or "/"), self.default_limit)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in self._METHODS:
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(scope["method"], scope.get("path", ""))
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await _send_problem(send, scope, 413, "Content Too Large", _too_large_detail(limit))
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # FastAPI re-raises HTTPException from body parsing (other errors become 400).
                    raise HTTPException(status_code=413, detail=_too_large_detail(limit))
            return message

        await self.app(scope, limited_receive, send)


def _too_large_detail(limit: int) -> str:
    return f"The request body exceeds the limit of {limit // (1024 * 1024) or 1} MB."


async def _send_problem(
    send: Send,
    scope: Scope,
    status: int,
    title: str,
    detail: str,
    request_id: str | None = None,
) -> None:
    body = json.dumps(
        {
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": scope.get("path"),
            "request_id": request_id or current_request_id(),
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", PROBLEM_CONTENT_TYPE.encode()),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})

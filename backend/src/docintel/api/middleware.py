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
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from docintel.api.problems import PROBLEM_CONTENT_TYPE
from docintel.core.context import request_id_var
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
    body = json.dumps(
        {
            "type": "about:blank",
            "title": "Internal Server Error",
            "status": 500,
            "detail": "An unexpected error occurred.",
            "instance": scope.get("path"),
            "request_id": request_id,
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", PROBLEM_CONTENT_TYPE.encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


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

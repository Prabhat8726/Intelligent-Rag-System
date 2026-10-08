"""BodySizeLimitMiddleware in isolation: the wrapped app must never see more than the limit."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request

from docintel.api.middleware import BodySizeLimitMiddleware
from docintel.api.problems import register_exception_handlers

LIMIT = 10_000


def _app(received: list[int]) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            received.append(total)
        return {"bytes": total}

    @app.post("/upload")
    async def upload(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    app.add_middleware(
        BodySizeLimitMiddleware, default_limit=LIMIT, overrides={("POST", "/upload"): LIMIT * 10}
    )
    return app


async def _post(app: FastAPI, path: str, content: bytes | AsyncIterator[bytes]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, content=content)


async def test_small_bodies_pass_through() -> None:
    response = await _post(_app([]), "/echo", b"x" * LIMIT)
    assert response.status_code == 200
    assert response.json() == {"bytes": LIMIT}


async def test_declared_length_over_limit_never_reaches_the_app() -> None:
    received: list[int] = []
    response = await _post(_app(received), "/echo", b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert received == []


async def test_streamed_body_is_cut_off_at_the_limit() -> None:
    received: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(100):
            yield b"x" * 1000  # 100 kB, no Content-Length

    response = await _post(_app(received), "/echo", chunks())
    assert response.status_code == 413
    assert max(received, default=0) <= LIMIT


@pytest.mark.parametrize(("size", "status"), [(LIMIT * 5, 200), (LIMIT * 10 + 1, 413)])
async def test_per_route_override(size: int, status: int) -> None:
    response = await _post(_app([]), "/upload", b"x" * size)
    assert response.status_code == status


async def test_invalid_content_length_is_rejected() -> None:
    app = _app([])

    async def send_raw() -> int:
        status: list[int] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.start":
                status.append(int(message["status"]))  # type: ignore[call-overload]

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/echo",
            "headers": [(b"content-length", b"-5")],
            "query_string": b"",
        }
        await app(scope, receive, send)  # type: ignore[arg-type]
        return status[0]

    assert await send_raw() == 413

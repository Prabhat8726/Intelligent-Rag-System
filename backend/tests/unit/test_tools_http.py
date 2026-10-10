"""The CLI clients wait out 429 responses as `Retry-After` says."""

from __future__ import annotations

import httpx

from docintel.tools.http import MAX_WAIT_SECONDS, RetryAfterTransport


def handler_refusing(times: int, retry_after: str = "7") -> tuple[list[bytes], httpx.MockTransport]:
    bodies: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if len(bodies) <= times:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(201, json={"ok": True})

    return bodies, httpx.MockTransport(handle)


async def test_a_refused_request_is_sent_again_with_its_body_after_the_wait() -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    bodies, inner = handler_refusing(2)
    transport = RetryAfterTransport(inner, sleep=sleep)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        response = await client.post("/api/v1/documents", files={"file": ("a.pdf", b"%PDF-1.7")})

    assert response.status_code == 201
    assert waits == [7.0, 7.0]
    assert len(bodies) == 3
    assert bodies[0] == bodies[2]
    assert b"%PDF-1.7" in bodies[2]


async def test_waits_are_bounded_and_retries_end() -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    _, inner = handler_refusing(100, retry_after="3600")
    transport = RetryAfterTransport(inner, max_retries=2, sleep=sleep)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        response = await client.get("/api/v1/search")

    assert response.status_code == 429  # the caller sees the refusal after the last retry
    assert waits == [MAX_WAIT_SECONDS, MAX_WAIT_SECONDS]


async def test_unparseable_retry_after_waits_one_second() -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    _, inner = handler_refusing(1, retry_after="Wed, 21 Oct 2015 07:28:00 GMT")
    transport = RetryAfterTransport(inner, sleep=sleep)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        assert (await client.get("/x")).status_code == 201
    assert waits == [1.0]

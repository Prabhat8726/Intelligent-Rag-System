"""HTTP transport for the command-line API clients (ingest, knowledge-ingest, demo, load test).

The API refuses requests over the per-caller limits with 429 and `Retry-After` (ADR-074). A
batch client uploading a dataset is expected to hit them, so it waits as told and sends the
request again, a bounded number of times, instead of failing the run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

MAX_RETRIES = 5
MAX_WAIT_SECONDS = 60.0


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After", "")
    seconds = float(value) if value.isdigit() else 1.0
    return min(max(seconds, 1.0), MAX_WAIT_SECONDS)


class RetryAfterTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None = None,
        *,
        max_retries: int = MAX_RETRIES,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._max_retries = max_retries
        self._sleep = sleep

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()  # the body is sent again on a retry
        for _ in range(self._max_retries):
            response = await self._inner.handle_async_request(request)
            if response.status_code != httpx.codes.TOO_MANY_REQUESTS:
                return response
            await response.aread()
            await response.aclose()
            await self._sleep(_retry_after(response))
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()

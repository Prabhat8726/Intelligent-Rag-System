"""Async token-bucket rate limiter.

Free-tier quotas are expressed as requests per minute. Waiting client-side is cheaper and more
predictable than provoking HTTP 429s and backing off.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class AsyncRateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if requests_per_minute < 1:
            msg = "requests_per_minute must be >= 1"
            raise ValueError(msg)
        self._capacity = float(requests_per_minute)
        self._refill_per_second = requests_per_minute / 60.0
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)
        self._updated = now

    async def acquire(self) -> None:
        """Wait until a request may be sent. Waiters are served one at a time (FIFO on the lock)."""
        async with self._lock:
            self._refill()
            while self._tokens < 1.0:
                await self._sleep((1.0 - self._tokens) / self._refill_per_second)
                self._refill()
            self._tokens -= 1.0

from __future__ import annotations

import pytest

from docintel.ai.rate_limit import AsyncRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_burst_up_to_capacity_then_waits_for_refill() -> None:
    clock = FakeClock()
    limiter = AsyncRateLimiter(6, clock=clock, sleep=clock.sleep)  # one token every 10 s
    for _ in range(6):
        await limiter.acquire()
    assert clock.sleeps == []

    await limiter.acquire()
    assert clock.sleeps == [pytest.approx(10.0)]


async def test_tokens_refill_with_elapsed_time() -> None:
    clock = FakeClock()
    limiter = AsyncRateLimiter(60, clock=clock, sleep=clock.sleep)  # one token per second
    for _ in range(60):
        await limiter.acquire()
    clock.now += 5.0
    for _ in range(5):
        await limiter.acquire()
    assert clock.sleeps == []


def test_invalid_rate_rejected() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        AsyncRateLimiter(0)

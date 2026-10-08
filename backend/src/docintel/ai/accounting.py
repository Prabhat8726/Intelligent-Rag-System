"""LLM usage accounting and the daily request budget (Modules 24, 43).

`AccountedLLMProvider` wraps any provider: before each call it asks the call log whether the
day's budget allows another request; after it, success or failure, it records provider, model,
purpose, document, tokens, latency and an estimated cost. Prompt and completion text are never
recorded. A failing log write never fails the model call (it is logged instead).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.ai.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ModelTier,
    StructuredLLMResponse,
)
from docintel.ai.errors import ProviderError
from docintel.core.logging import get_logger
from docintel.db.models import LLMCall, LLMCallStatus

logger = get_logger(__name__)

_MILLION = Decimal(1_000_000)


class ProviderBudgetExceededError(ProviderError):
    """The configured daily request budget is used up (LLM_DAILY_REQUEST_BUDGET)."""


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens. Thinking tokens are billed as output."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


@dataclass(frozen=True, slots=True)
class LLMCallEntry:
    provider: str
    model: str
    purpose: str
    status: LLMCallStatus
    latency_ms: float
    document_id: object | None = None
    prompt_version: str | None = None
    error_code: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    estimated_cost_usd: Decimal | None = None


class LLMCallLog(Protocol):
    async def check_budget(self, provider: str) -> None:
        """Raise ProviderBudgetExceededError if no request may be sent today."""
        ...

    async def record(self, entry: LLMCallEntry) -> None: ...


def estimate_cost(usage: LLMUsage, price: ModelPrice | None, *, local: bool) -> Decimal | None:
    """Cost at list price; 0 for local models; None when no price is configured."""
    if local:
        return Decimal(0)
    if price is None or usage.input_tokens is None:
        return None
    output = (usage.output_tokens or 0) + (usage.thinking_tokens or 0)
    cost = (
        Decimal(usage.input_tokens) * price.input_per_mtok + Decimal(output) * price.output_per_mtok
    ) / _MILLION
    return cost.quantize(Decimal("0.000001"))


class AccountedLLMProvider:
    """LLMProvider decorator that enforces the budget and records every call."""

    def __init__(
        self,
        inner: LLMProvider,
        log: LLMCallLog,
        prices: Mapping[str, ModelPrice] | None = None,
    ) -> None:
        self._inner = inner
        self._log = log
        self._prices = dict(prices or {})

    @property
    def inner(self) -> LLMProvider:
        return self._inner

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def supports_images(self) -> bool:
        return self._inner.supports_images

    @property
    def local(self) -> bool:
        return self._inner.local

    def model_for(self, tier: ModelTier) -> str:
        return self._inner.model_for(tier)

    async def _record(self, entry: LLMCallEntry) -> None:
        try:
            await self._log.record(entry)
        except Exception as exc:  # accounting must never break the call it accounts for
            logger.warning("ai.llm.accounting_failed", error_type=type(exc).__name__)

    async def _accounted[R](
        self,
        request: LLMRequest,
        call: Callable[[], Awaitable[R]],
        usage_of: Callable[[R], LLMUsage],
    ) -> R:
        await self._log.check_budget(self._inner.name)
        model = self._inner.model_for(request.tier)
        started = time.perf_counter()
        try:
            result = await call()
        except ProviderError as exc:
            await self._record(
                LLMCallEntry(
                    provider=self._inner.name,
                    model=model,
                    purpose=request.purpose,
                    status=LLMCallStatus.FAILED,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    document_id=request.document_id,
                    prompt_version=request.prompt_version,
                    error_code=type(exc).__name__,
                )
            )
            raise
        usage = usage_of(result)
        await self._record(
            LLMCallEntry(
                provider=usage.provider,
                model=usage.model,
                purpose=request.purpose,
                status=LLMCallStatus.SUCCEEDED,
                latency_ms=usage.latency_ms,
                document_id=request.document_id,
                prompt_version=request.prompt_version,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                thinking_tokens=usage.thinking_tokens,
                estimated_cost_usd=estimate_cost(
                    usage, self._prices.get(usage.model), local=self._inner.local
                ),
            )
        )
        return result

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return await self._accounted(
            request, lambda: self._inner.generate(request), lambda response: response.usage
        )

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredLLMResponse[T]:
        return await self._accounted(
            request,
            lambda: self._inner.generate_structured(request, schema),
            lambda response: response.usage,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


class DatabaseLLMCallLog:
    """`llm_calls` rows written in their own short transactions (a failed job keeps them)."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], *, daily_request_budget: int
    ) -> None:
        self._sessionmaker = sessionmaker
        self._budget = daily_request_budget

    async def calls_today(self, provider: str) -> int:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._sessionmaker() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(LLMCall)
                .where(LLMCall.provider == provider, LLMCall.created_at >= start)
            )
        return int(count or 0)

    async def check_budget(self, provider: str) -> None:
        if self._budget <= 0:
            return
        used = await self.calls_today(provider)
        if used >= self._budget:
            msg = (
                f"daily LLM request budget reached ({used}/{self._budget}, "
                "LLM_DAILY_REQUEST_BUDGET); resets at 00:00 UTC"
            )
            raise ProviderBudgetExceededError(msg, provider=provider)

    async def record(self, entry: LLMCallEntry) -> None:
        async with self._sessionmaker() as session, session.begin():
            session.add(
                LLMCall(
                    provider=entry.provider,
                    model=entry.model,
                    purpose=entry.purpose[:60],
                    document_id=entry.document_id,
                    prompt_version=entry.prompt_version,
                    status=entry.status,
                    error_code=entry.error_code,
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                    thinking_tokens=entry.thinking_tokens,
                    latency_ms=Decimal(f"{entry.latency_ms:.2f}"),
                    estimated_cost_usd=entry.estimated_cost_usd,
                )
            )

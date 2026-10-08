"""Builds configured providers. The only place that knows which implementation is active."""

from __future__ import annotations

from typing import assert_never

from docintel.ai.base import EMBEDDING_DIMENSIONS, EmbeddingProvider, LLMProvider
from docintel.ai.gemini import GeminiEmbeddingProvider, GeminiLLMProvider
from docintel.ai.rate_limit import AsyncRateLimiter
from docintel.core.config import EmbeddingProviderName, LLMProviderName, Settings


def build_llm_provider(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case LLMProviderName.GEMINI:
            return GeminiLLMProvider(
                api_key=settings.gemini_api_key,
                model=settings.gemini_model,
                fast_model=settings.gemini_fast_model,
                rate_limiter=AsyncRateLimiter(settings.llm_requests_per_minute),
                timeout_seconds=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                thinking_level=settings.gemini_thinking_level,
                temperature=settings.llm_temperature,
            )
        case _:
            assert_never(settings.llm_provider)


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    match settings.embedding_provider:
        case EmbeddingProviderName.GEMINI:
            return GeminiEmbeddingProvider(
                api_key=settings.gemini_api_key,
                model=settings.gemini_embedding_model,
                rate_limiter=AsyncRateLimiter(settings.embedding_requests_per_minute),
                dimensions=EMBEDDING_DIMENSIONS,
                batch_size=settings.embedding_batch_size,
                timeout_seconds=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )
        case _:
            assert_never(settings.embedding_provider)

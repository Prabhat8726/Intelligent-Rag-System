"""Builds configured providers. The only place that knows which implementation is active."""

from __future__ import annotations

from typing import assert_never

from docintel.ai.accounting import ModelPrice
from docintel.ai.base import EMBEDDING_DIMENSIONS, EmbeddingProvider, LLMProvider
from docintel.ai.errors import ProviderConfigurationError
from docintel.ai.gemini import GeminiEmbeddingProvider, GeminiLLMProvider
from docintel.ai.ollama import OllamaLLMProvider
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
        case LLMProviderName.OLLAMA:
            if not settings.ollama_model:
                msg = "OLLAMA_MODEL is not set (the model you pulled with `ollama pull`)."
                raise ProviderConfigurationError(msg, provider="ollama")
            return OllamaLLMProvider(
                base_url=settings.ollama_base_url,
                model=settings.ollama_model,
                fast_model=settings.ollama_fast_model,
                vision=settings.ollama_vision,
                timeout_seconds=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                temperature=settings.llm_temperature,
                keep_alive=settings.ollama_keep_alive,
            )
        case _:
            assert_never(settings.llm_provider)


def model_prices(settings: Settings) -> dict[str, ModelPrice]:
    return {
        model: ModelPrice(price.input_per_mtok, price.output_per_mtok)
        for model, price in settings.llm_pricing.items()
    }


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

"""Gemini implementations of LLMProvider and EmbeddingProvider (google-genai SDK 2.x).

Verified against google-genai 2.29 (see docs/architecture/06-ai-architecture.md):
* async calls via `client.aio.models.generate_content / embed_content`
* structured output via `response_mime_type="application/json"` + `response_json_schema`
* retries via `HttpOptions.retry_options`, timeout in milliseconds
* sampling/thinking parameters are only sent when explicitly configured (Gemini 3.x guidance)

Prompt and completion text are never logged; only model, purpose, latency and token counts.
"""

from __future__ import annotations

import functools
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from itertools import batched
from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, SecretStr, ValidationError

from docintel.ai.base import (
    EMBEDDING_DIMENSIONS,
    EmbeddingResult,
    EmbeddingTask,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ModelTier,
    StructuredLLMResponse,
)
from docintel.ai.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
    StructuredOutputError,
)
from docintel.ai.rate_limit import AsyncRateLimiter
from docintel.ai.schema import json_schema_for, strip_code_fence
from docintel.core.logging import get_logger

PROVIDER_NAME = "gemini"
RETRYABLE_STATUS_CODES = (408, 429, 500, 502, 503, 504)
_MAX_RETRY_DELAY_SECONDS = 30.0

logger = get_logger(__name__)


def map_api_error(exc: genai_errors.APIError) -> ProviderError:
    """Translate SDK errors into the provider-neutral taxonomy."""
    code = exc.code or 0
    message = exc.message or exc.status or "request failed"
    if code == 429:
        return ProviderRateLimitError(
            f"Gemini rate limit or quota exceeded: {message}", provider=PROVIDER_NAME
        )
    if code in (401, 403) or "API_KEY_INVALID" in str(exc.details):
        return ProviderConfigurationError(
            "Gemini rejected the credentials or permissions; check GEMINI_API_KEY.",
            provider=PROVIDER_NAME,
        )
    if code == 404:
        return ProviderConfigurationError(
            f"Gemini model not found or not available to this API key: {message}",
            provider=PROVIDER_NAME,
        )
    if code == 408 or code >= 500:
        return ProviderUnavailableError(
            f"Gemini is unavailable ({code}): {message}", provider=PROVIDER_NAME
        )
    return ProviderRequestError(
        f"Gemini rejected the request ({code} {exc.status}): {message}", provider=PROVIDER_NAME
    )


def l2_normalize(values: Sequence[float]) -> list[float]:
    norm = math.sqrt(math.fsum(v * v for v in values))
    if norm == 0.0:
        msg = "cannot normalize a zero vector"
        raise ValueError(msg)
    return [v / norm for v in values]


class _GeminiClient:
    """Shared client construction, rate limiting and error mapping."""

    def __init__(
        self,
        *,
        api_key: SecretStr | str | None,
        timeout_seconds: float,
        max_retries: int,
        rate_limiter: AsyncRateLimiter,
        http_client: httpx.AsyncClient | None = None,
        retry_initial_delay_seconds: float = 1.0,
    ) -> None:
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not key:
            msg = (
                "GEMINI_API_KEY is not set. Add it to .env (see docs/development/configuration.md)."
            )
            raise ProviderConfigurationError(msg, provider=PROVIDER_NAME)
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient()
        self._limiter = rate_limiter
        self._client = genai.Client(
            vertexai=False,
            api_key=key,
            http_options=types.HttpOptions(
                timeout=int(timeout_seconds * 1000),
                httpx_async_client=self._http,
                retry_options=types.HttpRetryOptions(
                    attempts=max_retries + 1,
                    initial_delay=retry_initial_delay_seconds,
                    max_delay=_MAX_RETRY_DELAY_SECONDS,
                    exp_base=2.0,
                    jitter=1.0,
                    http_status_codes=list(RETRYABLE_STATUS_CODES),
                ),
            ),
        )

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    async def _call[R](self, operation: Callable[[], Awaitable[R]]) -> R:
        await self._limiter.acquire()
        try:
            return await operation()
        except genai_errors.APIError as exc:
            raise map_api_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(
                "Gemini request timed out", provider=PROVIDER_NAME
            ) from exc
        except httpx.TransportError as exc:
            msg = f"Could not reach Gemini: {type(exc).__name__}"
            raise ProviderUnavailableError(msg, provider=PROVIDER_NAME) from exc

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()


class GeminiLLMProvider(_GeminiClient):
    def __init__(
        self,
        *,
        api_key: SecretStr | str | None,
        model: str,
        fast_model: str,
        rate_limiter: AsyncRateLimiter,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        thinking_level: str | None = None,
        temperature: float | None = None,
        http_client: httpx.AsyncClient | None = None,
        retry_initial_delay_seconds: float = 1.0,
    ) -> None:
        super().__init__(
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            rate_limiter=rate_limiter,
            http_client=http_client,
            retry_initial_delay_seconds=retry_initial_delay_seconds,
        )
        self._models = {ModelTier.DEFAULT: model, ModelTier.FAST: fast_model}
        self._thinking_level = thinking_level
        self._temperature = temperature

    def model_for(self, tier: ModelTier) -> str:
        return self._models[tier]

    @property
    def supports_images(self) -> bool:
        return True

    @property
    def local(self) -> bool:
        return False

    def _config(
        self, request: LLMRequest, json_schema: dict[str, Any] | None
    ) -> types.GenerateContentConfig:
        options: dict[str, Any] = {}
        if request.system_instruction:
            options["system_instruction"] = request.system_instruction
        if request.max_output_tokens is not None:
            options["max_output_tokens"] = request.max_output_tokens
        if self._temperature is not None:
            options["temperature"] = self._temperature
        if self._thinking_level is not None:
            options["thinking_config"] = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(self._thinking_level)
            )
        if json_schema is not None:
            options["response_mime_type"] = "application/json"
            options["response_json_schema"] = json_schema
        return types.GenerateContentConfig(**options)

    async def _generate(
        self, request: LLMRequest, json_schema: dict[str, Any] | None
    ) -> tuple[str, LLMUsage, str | None]:
        model = self.model_for(request.tier)
        contents: list[types.PartUnion] = [
            types.Part.from_bytes(data=image.data, mime_type=image.mime_type)
            for image in request.images
        ]
        contents.append(request.prompt)
        call = functools.partial(
            self._client.aio.models.generate_content,
            model=model,
            contents=contents if request.images else request.prompt,
            config=self._config(request, json_schema),
        )
        started = time.perf_counter()
        try:
            response = await self._call(call)
        except ProviderError as exc:
            logger.warning(
                "ai.llm.call",
                provider=PROVIDER_NAME,
                model=model,
                purpose=request.purpose,
                status="error",
                error_type=type(exc).__name__,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        latency_ms = round((time.perf_counter() - started) * 1000, 2)

        metadata = response.usage_metadata
        usage = LLMUsage(
            provider=PROVIDER_NAME,
            model=model,
            latency_ms=latency_ms,
            input_tokens=metadata.prompt_token_count if metadata else None,
            output_tokens=metadata.candidates_token_count if metadata else None,
            thinking_tokens=metadata.thoughts_token_count if metadata else None,
            total_tokens=metadata.total_token_count if metadata else None,
        )
        candidate = response.candidates[0] if response.candidates else None
        finish_reason = (
            str(candidate.finish_reason.value) if candidate and candidate.finish_reason else None
        )
        logger.info(
            "ai.llm.call",
            provider=PROVIDER_NAME,
            model=model,
            purpose=request.purpose,
            status="ok",
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            thinking_tokens=usage.thinking_tokens,
        )

        text = response.text
        if not text:
            feedback = response.prompt_feedback
            block_reason = (
                feedback.block_reason.value if feedback and feedback.block_reason else None
            )
            msg = (
                f"Gemini returned no text (block_reason={block_reason}, "
                f"finish_reason={finish_reason})"
            )
            raise ProviderResponseError(msg, provider=PROVIDER_NAME)
        return text, usage, finish_reason

    async def generate(self, request: LLMRequest) -> LLMResponse:
        text, usage, finish_reason = await self._generate(request, json_schema=None)
        return LLMResponse(text=text, usage=usage, finish_reason=finish_reason)

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredLLMResponse[T]:
        text, usage, finish_reason = await self._generate(
            request, json_schema=json_schema_for(schema)
        )
        try:
            data = schema.model_validate_json(strip_code_fence(text))
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
                for error in exc.errors(include_input=False)
            ]
            msg = (
                f"Gemini output did not match schema {schema.__name__} "
                f"(finish_reason={finish_reason}, {len(errors)} error(s))"
            )
            raise StructuredOutputError(
                msg, provider=PROVIDER_NAME, raw_text=text, validation_errors=errors
            ) from exc
        return StructuredLLMResponse(data=data, raw_text=text, usage=usage)

    async def list_generation_models(self) -> list[str]:
        """Model ids this API key can use for generateContent (diagnostics for `check-ai`)."""

        async def _collect() -> list[str]:
            names: list[str] = []
            async for model in await self._client.aio.models.list():
                actions = model.supported_actions or []
                if model.name and "generateContent" in actions:
                    names.append(model.name.removeprefix("models/"))
            return sorted(names)

        return await self._call(_collect)


class GeminiEmbeddingProvider(_GeminiClient):
    local = False  # an external service: content above AI_EXTERNAL_MAX_SENSITIVITY stays out

    def __init__(
        self,
        *,
        api_key: SecretStr | str | None,
        model: str,
        rate_limiter: AsyncRateLimiter,
        dimensions: int = EMBEDDING_DIMENSIONS,
        batch_size: int = 100,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        http_client: httpx.AsyncClient | None = None,
        retry_initial_delay_seconds: float = 1.0,
    ) -> None:
        super().__init__(
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            rate_limiter=rate_limiter,
            http_client=http_client,
            retry_initial_delay_seconds=retry_initial_delay_seconds,
        )
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str], task: EmbeddingTask) -> EmbeddingResult:
        if any(not text.strip() for text in texts):
            msg = "cannot embed empty or whitespace-only text"
            raise ValueError(msg)
        started = time.perf_counter()
        vectors: list[list[float]] = []
        config = types.EmbedContentConfig(
            task_type=task.value, output_dimensionality=self._dimensions
        )
        for batch in batched(texts, self._batch_size, strict=False):
            contents: list[types.PartUnion] = list(batch)
            call = functools.partial(
                self._client.aio.models.embed_content,
                model=self._model,
                contents=contents,
                config=config,
            )
            response = await self._call(call)
            embeddings = response.embeddings or []
            if len(embeddings) != len(batch):
                msg = f"Gemini returned {len(embeddings)} embeddings for {len(batch)} inputs"
                raise ProviderResponseError(msg, provider=PROVIDER_NAME)
            for embedding in embeddings:
                values = embedding.values or []
                if len(values) != self._dimensions:
                    msg = f"expected {self._dimensions}-d embedding, got {len(values)}-d"
                    raise ProviderResponseError(msg, provider=PROVIDER_NAME)
                try:
                    vectors.append(l2_normalize(values))
                except ValueError as exc:
                    raise ProviderResponseError(str(exc), provider=PROVIDER_NAME) from exc
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "ai.embedding.call",
            provider=PROVIDER_NAME,
            model=self._model,
            task=task.value,
            count=len(texts),
            latency_ms=latency_ms,
        )
        return EmbeddingResult(
            vectors=vectors, model=self._model, dimensions=self._dimensions, latency_ms=latency_ms
        )

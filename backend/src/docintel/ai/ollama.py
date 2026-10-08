"""Ollama implementation of LLMProvider: a self-hosted model server (Module 43, local option).

Verified against Ollama's API reference (docs/api.md in github.com/ollama/ollama, 2026-10-08):
* `POST /api/chat` with `model`, `messages` (`role`, `content`, base64 `images`), `format`
  (a JSON schema object for structured output), `stream: false`, `options`
  (`temperature`, `num_predict`)
* non-streaming response: `message.content`, `done_reason`, `prompt_eval_count`,
  `eval_count`, `total_duration` (nanoseconds)
* `GET /api/tags` lists the models pulled on the server

Content sent here stays on infrastructure the operator runs, so the provider reports
`local = True` and the external-AI sensitivity gate does not block it (ADR-029). Point
OLLAMA_BASE_URL only at a server inside the deployment's trust boundary.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from docintel.ai.base import LLMRequest, LLMResponse, LLMUsage, ModelTier, StructuredLLMResponse
from docintel.ai.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
    StructuredOutputError,
)
from docintel.ai.schema import json_schema_for, strip_code_fence
from docintel.core.logging import get_logger

PROVIDER_NAME = "ollama"
_RETRY_BASE_SECONDS = 1.0
_NANOSECONDS = 1_000_000_000

logger = get_logger(__name__)


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return str(payload["error"])[:300]
    return response.text[:200]


def map_status(response: httpx.Response, model: str) -> ProviderError:
    message = _error_message(response)
    if response.status_code == 404:
        return ProviderConfigurationError(
            f"Ollama model '{model}' is not available on the server ({message}); "
            f"run `ollama pull {model}`.",
            provider=PROVIDER_NAME,
        )
    if response.status_code in (408, 429) or response.status_code >= 500:
        return ProviderUnavailableError(
            f"Ollama is unavailable ({response.status_code}): {message}", provider=PROVIDER_NAME
        )
    return ProviderRequestError(
        f"Ollama rejected the request ({response.status_code}): {message}", provider=PROVIDER_NAME
    )


class OllamaLLMProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        fast_model: str | None = None,
        vision: bool = False,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        temperature: float | None = None,
        keep_alive: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        retry_base_seconds: float = _RETRY_BASE_SECONDS,
    ) -> None:
        self._models = {ModelTier.DEFAULT: model, ModelTier.FAST: fast_model or model}
        self._vision = vision
        self._max_retries = max_retries
        self._temperature = temperature
        self._keep_alive = keep_alive
        self._retry_base = retry_base_seconds
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def supports_images(self) -> bool:
        return self._vision

    @property
    def local(self) -> bool:
        return True

    def model_for(self, tier: ModelTier) -> str:
        return self._models[tier]

    def _payload(self, request: LLMRequest, json_schema: dict[str, Any] | None) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if request.system_instruction:
            messages.append({"role": "system", "content": request.system_instruction})
        user: dict[str, Any] = {"role": "user", "content": request.prompt}
        if request.images and self._vision:
            user["images"] = [base64.b64encode(image.data).decode() for image in request.images]
        messages.append(user)
        options: dict[str, Any] = {}
        if self._temperature is not None:
            options["temperature"] = self._temperature
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens
        payload: dict[str, Any] = {
            "model": self.model_for(request.tier),
            "messages": messages,
            "stream": False,
        }
        if options:
            payload["options"] = options
        if json_schema is not None:
            payload["format"] = json_schema
        if self._keep_alive is not None:
            payload["keep_alive"] = self._keep_alive
        return payload

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = str(payload["model"])
        attempt = 0
        while True:
            try:
                response = await self._http.post("/api/chat", json=payload)
            except httpx.TimeoutException as exc:
                error: ProviderError = ProviderUnavailableError(
                    "Ollama request timed out", provider=PROVIDER_NAME
                )
                cause: Exception = exc
            except httpx.TransportError as exc:
                error = ProviderUnavailableError(
                    f"Could not reach Ollama ({type(exc).__name__}); is the server running?",
                    provider=PROVIDER_NAME,
                )
                cause = exc
            else:
                if response.is_success:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        msg = "Ollama returned a non-JSON response"
                        raise ProviderResponseError(msg, provider=PROVIDER_NAME) from exc
                    if not isinstance(data, dict):
                        msg = "Ollama returned an unexpected response shape"
                        raise ProviderResponseError(msg, provider=PROVIDER_NAME)
                    return data
                error, cause = (
                    map_status(response, model),
                    httpx.HTTPStatusError("status", request=response.request, response=response),
                )
            if not error.retryable or attempt >= self._max_retries:
                raise error from cause
            await asyncio.sleep(self._retry_base * 2**attempt)
            attempt += 1

    async def _generate(
        self, request: LLMRequest, json_schema: dict[str, Any] | None
    ) -> tuple[str, LLMUsage, str | None]:
        payload = self._payload(request, json_schema)
        model = str(payload["model"])
        started = time.perf_counter()
        try:
            data = await self._post(payload)
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
        message = data.get("message") or {}
        text = message.get("content") if isinstance(message, dict) else None
        prompt_tokens = data.get("prompt_eval_count")
        output_tokens = data.get("eval_count")
        usage = LLMUsage(
            provider=PROVIDER_NAME,
            model=model,
            latency_ms=latency_ms,
            input_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            total_tokens=(
                prompt_tokens + output_tokens
                if isinstance(prompt_tokens, int) and isinstance(output_tokens, int)
                else None
            ),
        )
        finish_reason = data.get("done_reason")
        logger.info(
            "ai.llm.call",
            provider=PROVIDER_NAME,
            model=model,
            purpose=request.purpose,
            status="ok",
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            server_ms=(
                round(data["total_duration"] / _NANOSECONDS * 1000, 2)
                if isinstance(data.get("total_duration"), int)
                else None
            ),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        if not isinstance(text, str) or not text.strip():
            msg = f"Ollama returned no text (done_reason={finish_reason})"
            raise ProviderResponseError(msg, provider=PROVIDER_NAME)
        return text, usage, finish_reason if isinstance(finish_reason, str) else None

    async def generate(self, request: LLMRequest) -> LLMResponse:
        text, usage, finish_reason = await self._generate(request, json_schema=None)
        return LLMResponse(text=text, usage=usage, finish_reason=finish_reason)

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredLLMResponse[T]:
        text, usage, finish_reason = await self._generate(request, json_schema_for(schema))
        try:
            data = schema.model_validate_json(strip_code_fence(text))
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
                for error in exc.errors(include_input=False)
            ]
            msg = (
                f"Ollama output did not match schema {schema.__name__} "
                f"(finish_reason={finish_reason}, {len(errors)} error(s))"
            )
            raise StructuredOutputError(
                msg, provider=PROVIDER_NAME, raw_text=text, validation_errors=errors
            ) from exc
        return StructuredLLMResponse(data=data, raw_text=text, usage=usage)

    async def list_models(self) -> list[str]:
        """Models pulled on the server (diagnostics for `check-ai`)."""
        try:
            response = await self._http.get("/api/tags")
        except httpx.TransportError as exc:
            msg = f"Could not reach Ollama ({type(exc).__name__}); is the server running?"
            raise ProviderUnavailableError(msg, provider=PROVIDER_NAME) from exc
        if not response.is_success:
            raise map_status(response, "-")
        models = response.json().get("models", [])
        return sorted(
            str(model["name"]) for model in models if isinstance(model, dict) and "name" in model
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

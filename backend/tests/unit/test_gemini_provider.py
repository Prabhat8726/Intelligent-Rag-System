"""Gemini providers tested through the real google-genai SDK with HTTP mocked at the transport.

This exercises our request construction, the SDK's serialization and retry logic, and our
response/error handling - without network access or an API key.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel

from docintel.ai.base import EmbeddingTask, LLMRequest, ModelTier
from docintel.ai.errors import (
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
    StructuredOutputError,
)
from docintel.ai.gemini import GeminiEmbeddingProvider, GeminiLLMProvider, l2_normalize
from docintel.ai.rate_limit import AsyncRateLimiter
from docintel.ai.registry import build_embedding_provider, build_llm_provider
from tests.conftest import make_settings

API = "https://generativelanguage.googleapis.com/v1beta"
API_KEY = "test-api-key-not-real"


class InvoiceTotals(BaseModel):
    invoice_number: str
    total: float


def _generate_payload(text: str | None, *, finish: str = "STOP") -> dict[str, Any]:
    if text is None:
        return {"promptFeedback": {"blockReason": "SAFETY"}}
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}
        ],
        "usageMetadata": {
            "promptTokenCount": 120,
            "candidatesTokenCount": 30,
            "thoughtsTokenCount": 12,
            "totalTokenCount": 162,
        },
    }


def _error(code: int, status: str, message: str, reason: str | None = None) -> httpx.Response:
    error: dict[str, Any] = {"code": code, "status": status, "message": message}
    if reason:
        error["details"] = [{"reason": reason}]
    return httpx.Response(code, json={"error": error})


@pytest.fixture
async def mock_api() -> AsyncIterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


def _llm(**overrides: Any) -> GeminiLLMProvider:
    options: dict[str, Any] = {
        "api_key": API_KEY,
        "model": "gemini-main",
        "fast_model": "gemini-fast",
        "rate_limiter": AsyncRateLimiter(1000),
        "max_retries": 0,
        "timeout_seconds": 5,
        "retry_initial_delay_seconds": 0.01,
    }
    options.update(overrides)
    return GeminiLLMProvider(**options)


def _embedder(**overrides: Any) -> GeminiEmbeddingProvider:
    options: dict[str, Any] = {
        "api_key": API_KEY,
        "model": "gemini-embedding-001",
        "rate_limiter": AsyncRateLimiter(1000),
        "dimensions": 4,
        "batch_size": 2,
        "max_retries": 0,
        "timeout_seconds": 5,
    }
    options.update(overrides)
    return GeminiEmbeddingProvider(**options)


# ------------------------------------------------------------------------------ generation
async def test_structured_generation_request_and_parsing(mock_api: respx.MockRouter) -> None:
    route = mock_api.post("/models/gemini-main:generateContent").respond(
        json=_generate_payload('{"invoice_number": "INV-7", "total": 1250.5}')
    )
    llm = _llm()
    result = await llm.generate_structured(
        LLMRequest(prompt="extract", system_instruction="be precise", purpose="test"),
        InvoiceTotals,
    )
    await llm.aclose()

    assert result.data == InvoiceTotals(invoice_number="INV-7", total=1250.5)
    assert result.usage.model == "gemini-main"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (120, 30)
    assert result.usage.thinking_tokens == 12

    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == API_KEY
    body = json.loads(request.content)
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"]["required"] == ["invoice_number", "total"]
    assert body["systemInstruction"]["parts"][0]["text"] == "be precise"
    # Gemini 3.x: sampling/thinking parameters are not sent unless configured.
    assert "temperature" not in config
    assert "thinkingConfig" not in config


async def test_configured_temperature_and_thinking_level_are_sent(
    mock_api: respx.MockRouter,
) -> None:
    route = mock_api.post("/models/gemini-fast:generateContent").respond(
        json=_generate_payload("plain answer")
    )
    llm = _llm(temperature=0.2, thinking_level="LOW")
    response = await llm.generate(LLMRequest(prompt="hi", tier=ModelTier.FAST))
    await llm.aclose()

    assert response.text == "plain answer"
    assert response.finish_reason == "STOP"
    config = json.loads(route.calls.last.request.content)["generationConfig"]
    assert config["temperature"] == 0.2
    assert "LOW" in json.dumps(config["thinkingConfig"])


async def test_code_fenced_json_is_accepted(mock_api: respx.MockRouter) -> None:
    fenced = '```json\n{"invoice_number": "A1", "total": 3}\n```'
    mock_api.post("/models/gemini-main:generateContent").respond(json=_generate_payload(fenced))
    llm = _llm()
    result = await llm.generate_structured(LLMRequest(prompt="x"), InvoiceTotals)
    await llm.aclose()
    assert result.data.invoice_number == "A1"


@pytest.mark.parametrize(
    "text",
    [
        '{"invoice_number": "INV-7", "total": ',  # truncated JSON
        '{"invoice_number": "INV-7"}',  # missing required field
        '{"invoice_number": "INV-7", "total": "a lot"}',  # wrong type
    ],
)
async def test_malformed_structured_output_raises_with_raw_text(
    mock_api: respx.MockRouter, text: str
) -> None:
    mock_api.post("/models/gemini-main:generateContent").respond(json=_generate_payload(text))
    llm = _llm()
    with pytest.raises(StructuredOutputError) as exc_info:
        await llm.generate_structured(LLMRequest(prompt="x"), InvoiceTotals)
    await llm.aclose()
    assert exc_info.value.raw_text == text
    assert exc_info.value.validation_errors


async def test_blocked_prompt_raises_response_error(mock_api: respx.MockRouter) -> None:
    mock_api.post("/models/gemini-main:generateContent").respond(json=_generate_payload(None))
    llm = _llm()
    with pytest.raises(ProviderResponseError, match="SAFETY"):
        await llm.generate(LLMRequest(prompt="x"))
    await llm.aclose()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_error(429, "RESOURCE_EXHAUSTED", "quota"), ProviderRateLimitError),
        (
            _error(400, "INVALID_ARGUMENT", "API key not valid", "API_KEY_INVALID"),
            ProviderConfigurationError,
        ),
        (_error(403, "PERMISSION_DENIED", "denied"), ProviderConfigurationError),
        (_error(404, "NOT_FOUND", "models/gemini-main is not found"), ProviderConfigurationError),
        (_error(400, "INVALID_ARGUMENT", "bad field"), ProviderRequestError),
        (_error(500, "INTERNAL", "boom"), ProviderUnavailableError),
    ],
)
async def test_http_errors_are_mapped(
    mock_api: respx.MockRouter, response: httpx.Response, expected: type[Exception]
) -> None:
    mock_api.post("/models/gemini-main:generateContent").mock(return_value=response)
    llm = _llm()
    with pytest.raises(expected):
        await llm.generate(LLMRequest(prompt="x"))
    await llm.aclose()


async def test_transient_errors_are_retried(mock_api: respx.MockRouter) -> None:
    route = mock_api.post("/models/gemini-main:generateContent").mock(
        side_effect=[
            _error(503, "UNAVAILABLE", "overloaded"),
            _error(429, "RESOURCE_EXHAUSTED", "slow down"),
            httpx.Response(200, json=_generate_payload("recovered")),
        ]
    )
    llm = _llm(max_retries=2)
    response = await llm.generate(LLMRequest(prompt="x"))
    await llm.aclose()
    assert response.text == "recovered"
    assert route.call_count == 3


async def test_retries_are_bounded(mock_api: respx.MockRouter) -> None:
    route = mock_api.post("/models/gemini-main:generateContent").mock(
        return_value=_error(503, "UNAVAILABLE", "still down")
    )
    llm = _llm(max_retries=1)
    with pytest.raises(ProviderUnavailableError):
        await llm.generate(LLMRequest(prompt="x"))
    await llm.aclose()
    assert route.call_count == 2


async def test_timeout_maps_to_unavailable(mock_api: respx.MockRouter) -> None:
    mock_api.post("/models/gemini-main:generateContent").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    llm = _llm()
    with pytest.raises(ProviderUnavailableError, match="timed out"):
        await llm.generate(LLMRequest(prompt="x"))
    await llm.aclose()


async def test_list_generation_models_filters_supported_actions(
    mock_api: respx.MockRouter,
) -> None:
    mock_api.get("/models").respond(
        json={
            "models": [
                {"name": "models/gemini-main", "supportedGenerationMethods": ["generateContent"]},
                {
                    "name": "models/gemini-embedding-001",
                    "supportedGenerationMethods": ["embedContent"],
                },
                {
                    "name": "models/gemini-fast",
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                },
            ]
        }
    )
    llm = _llm()
    assert await llm.list_generation_models() == ["gemini-fast", "gemini-main"]
    await llm.aclose()


def test_missing_api_key_is_a_configuration_error() -> None:
    with pytest.raises(ProviderConfigurationError, match="GEMINI_API_KEY"):
        _llm(api_key=None)
    with pytest.raises(ProviderConfigurationError):
        build_llm_provider(make_settings(gemini_api_key=None))
    with pytest.raises(ProviderConfigurationError):
        build_embedding_provider(make_settings(gemini_api_key=""))


async def test_registry_builds_configured_models() -> None:
    llm = build_llm_provider(
        make_settings(gemini_api_key=API_KEY, gemini_model="m-default", gemini_fast_model="m-fast")
    )
    assert llm.name == "gemini"
    assert llm.model_for(ModelTier.DEFAULT) == "m-default"
    assert llm.model_for(ModelTier.FAST) == "m-fast"
    await llm.aclose()
    embedder = build_embedding_provider(make_settings(gemini_api_key=API_KEY))
    assert embedder.dimensions == 768
    await embedder.aclose()


# ------------------------------------------------------------------------------ embeddings
async def test_embeddings_are_batched_normalized_and_typed(mock_api: respx.MockRouter) -> None:
    route = mock_api.post("/models/gemini-embedding-001:batchEmbedContents").mock(
        side_effect=[
            httpx.Response(
                200, json={"embeddings": [{"values": [3, 4, 0, 0]}, {"values": [0, 0, 0, 2]}]}
            ),
            httpx.Response(200, json={"embeddings": [{"values": [1, 1, 1, 1]}]}),
        ]
    )
    embedder = _embedder()
    result = await embedder.embed(["a", "b", "c"], EmbeddingTask.RETRIEVAL_DOCUMENT)
    await embedder.aclose()

    assert route.call_count == 2  # batch_size=2 → [a, b], [c]
    assert result.vectors[0] == pytest.approx([0.6, 0.8, 0.0, 0.0])
    assert result.vectors[1] == pytest.approx([0.0, 0.0, 0.0, 1.0])
    for vector in result.vectors:
        assert math.isclose(math.fsum(v * v for v in vector), 1.0)
    first_request = json.loads(route.calls[0].request.content)["requests"]
    assert [r["taskType"] for r in first_request] == ["RETRIEVAL_DOCUMENT"] * 2
    assert {r["outputDimensionality"] for r in first_request} == {4}


async def test_embedding_dimension_mismatch_is_rejected(mock_api: respx.MockRouter) -> None:
    mock_api.post("/models/gemini-embedding-001:batchEmbedContents").respond(
        json={"embeddings": [{"values": [1.0, 2.0]}]}
    )
    embedder = _embedder()
    with pytest.raises(ProviderResponseError, match="expected 4-d"):
        await embedder.embed(["a"], EmbeddingTask.RETRIEVAL_QUERY)
    await embedder.aclose()


async def test_embedding_count_mismatch_is_rejected(mock_api: respx.MockRouter) -> None:
    mock_api.post("/models/gemini-embedding-001:batchEmbedContents").respond(
        json={"embeddings": [{"values": [1, 0, 0, 0]}]}
    )
    embedder = _embedder()
    with pytest.raises(ProviderResponseError, match="1 embeddings for 2 inputs"):
        await embedder.embed(["a", "b"], EmbeddingTask.RETRIEVAL_DOCUMENT)
    await embedder.aclose()


async def test_empty_text_is_rejected_before_any_call(mock_api: respx.MockRouter) -> None:
    route = mock_api.post("/models/gemini-embedding-001:batchEmbedContents")
    embedder = _embedder()
    with pytest.raises(ValueError, match="empty"):
        await embedder.embed(["valid", "   "], EmbeddingTask.RETRIEVAL_DOCUMENT)
    await embedder.aclose()
    assert route.call_count == 0


def test_l2_normalize_rejects_zero_vector() -> None:
    with pytest.raises(ValueError, match="zero vector"):
        l2_normalize([0.0, 0.0])

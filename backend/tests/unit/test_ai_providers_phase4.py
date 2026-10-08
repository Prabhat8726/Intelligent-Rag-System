"""Ollama provider (HTTP mocked per its documented API), Gemini image input, call accounting
and the external-AI gate for local models."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel

from docintel.ai.accounting import (
    AccountedLLMProvider,
    LLMCallEntry,
    ModelPrice,
    ProviderBudgetExceededError,
    estimate_cost,
)
from docintel.ai.base import ImageInput, LLMRequest, LLMUsage, ModelTier
from docintel.ai.errors import (
    ProviderConfigurationError,
    ProviderRequestError,
    ProviderUnavailableError,
    StructuredOutputError,
)
from docintel.ai.gemini import GeminiLLMProvider
from docintel.ai.ollama import OllamaLLMProvider
from docintel.ai.rate_limit import AsyncRateLimiter
from docintel.ai.registry import build_llm_provider, model_prices
from docintel.ai.routing import ExternalAIGate
from docintel.db.models import LLMCallStatus, Sensitivity
from tests.conftest import make_settings

OLLAMA = "http://ollama.test:11434"


class Totals(BaseModel):
    invoice_number: str
    total: float


def _chat(content: str, *, prompt_tokens: int = 50, output_tokens: int = 9) -> dict[str, Any]:
    return {
        "model": "vlm:7b",
        "created_at": "2026-10-08T12:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "total_duration": 2_500_000_000,
        "prompt_eval_count": prompt_tokens,
        "eval_count": output_tokens,
    }


@pytest.fixture
async def ollama_api() -> AsyncIterator[respx.MockRouter]:
    with respx.mock(base_url=OLLAMA, assert_all_called=False) as router:
        yield router


def _ollama(**overrides: Any) -> OllamaLLMProvider:
    options: dict[str, Any] = {
        "base_url": OLLAMA,
        "model": "vlm:7b",
        "fast_model": "small:1b",
        "vision": True,
        "max_retries": 1,
        "retry_base_seconds": 0.0,
    }
    options.update(overrides)
    return OllamaLLMProvider(**options)


# ------------------------------------------------------------------------------ Ollama
async def test_ollama_structured_request_follows_the_chat_api(ollama_api: respx.MockRouter) -> None:
    route = ollama_api.post("/api/chat").respond(
        json=_chat('{"invoice_number": "INV-7", "total": 12.5}')
    )
    llm = _ollama(temperature=0.0)
    result = await llm.generate_structured(
        LLMRequest(
            prompt="extract",
            system_instruction="be precise",
            max_output_tokens=300,
            images=(ImageInput(b"png-bytes"),),
        ),
        Totals,
    )
    await llm.aclose()
    assert result.data == Totals(invoice_number="INV-7", total=12.5)
    assert (result.usage.input_tokens, result.usage.output_tokens) == (50, 9)
    assert result.usage.total_tokens == 59
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "vlm:7b"
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "be precise"}
    assert body["messages"][1]["images"] == [base64.b64encode(b"png-bytes").decode()]
    assert body["format"]["required"] == ["invoice_number", "total"]
    assert body["options"] == {"temperature": 0.0, "num_predict": 300}
    assert llm.local is True
    assert llm.supports_images is True


async def test_ollama_text_only_models_get_no_images(ollama_api: respx.MockRouter) -> None:
    route = ollama_api.post("/api/chat").respond(json=_chat("hello"))
    llm = _ollama(vision=False)
    response = await llm.generate(
        LLMRequest(prompt="hi", tier=ModelTier.FAST, images=(ImageInput(b"x"),))
    )
    await llm.aclose()
    assert response.text == "hello"
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "small:1b"
    assert "images" not in body["messages"][0]
    assert "format" not in body


async def test_ollama_invalid_json_is_a_structured_output_error(
    ollama_api: respx.MockRouter,
) -> None:
    ollama_api.post("/api/chat").respond(json=_chat('{"invoice_number": "INV-7"}'))
    llm = _ollama()
    with pytest.raises(StructuredOutputError) as caught:
        await llm.generate_structured(LLMRequest(prompt="x"), Totals)
    await llm.aclose()
    assert caught.value.validation_errors == ["total: Field required"]


async def test_ollama_errors_map_to_the_taxonomy(ollama_api: respx.MockRouter) -> None:
    llm = _ollama()
    ollama_api.post("/api/chat").respond(404, json={"error": "model 'vlm:7b' not found"})
    with pytest.raises(ProviderConfigurationError, match="ollama pull vlm:7b"):
        await llm.generate(LLMRequest(prompt="x"))
    ollama_api.post("/api/chat").respond(400, json={"error": "invalid format"})
    with pytest.raises(ProviderRequestError, match="invalid format"):
        await llm.generate(LLMRequest(prompt="x"))
    ollama_api.post("/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderUnavailableError, match="is the server running"):
        await llm.generate(LLMRequest(prompt="x"))
    await llm.aclose()


async def test_ollama_retries_overload_then_succeeds(ollama_api: respx.MockRouter) -> None:
    route = ollama_api.post("/api/chat").mock(
        side_effect=[
            httpx.Response(503, json={"error": "busy"}),
            httpx.Response(200, json=_chat("ok")),
        ]
    )
    llm = _ollama()
    response = await llm.generate(LLMRequest(prompt="x"))
    await llm.aclose()
    assert response.text == "ok"
    assert route.call_count == 2


async def test_ollama_lists_pulled_models(ollama_api: respx.MockRouter) -> None:
    ollama_api.get("/api/tags").respond(json={"models": [{"name": "b:1"}, {"name": "a:7b"}]})
    llm = _ollama()
    assert await llm.list_models() == ["a:7b", "b:1"]
    await llm.aclose()


def test_registry_builds_ollama_only_with_a_model() -> None:
    settings = make_settings(llm_provider="ollama", ollama_model=None)
    assert settings.llm_configured is False
    with pytest.raises(ProviderConfigurationError, match="OLLAMA_MODEL"):
        build_llm_provider(settings)
    configured = make_settings(llm_provider="ollama", ollama_model="vlm:7b", ollama_vision=True)
    assert configured.llm_configured is True
    provider = build_llm_provider(configured)
    assert isinstance(provider, OllamaLLMProvider)
    assert provider.supports_images is True


# ------------------------------------------------------------------------------ Gemini images
async def test_gemini_sends_images_as_inline_parts() -> None:
    api = "https://generativelanguage.googleapis.com/v1beta"
    payload = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "seen"}]}, "finishReason": "STOP"}
        ]
    }
    with respx.mock(base_url=api) as router:
        route = router.post("/models/gemini-main:generateContent").respond(json=payload)
        llm = GeminiLLMProvider(
            api_key="test-key-not-real",
            model="gemini-main",
            fast_model="gemini-fast",
            rate_limiter=AsyncRateLimiter(1000),
            max_retries=0,
        )
        await llm.generate(LLMRequest(prompt="read", images=(ImageInput(b"\x89PNG"),)))
        await llm.aclose()
    parts = json.loads(route.calls.last.request.content)["contents"][0]["parts"]
    inline = parts[0]["inlineData"]  # serialized by the official SDK
    assert inline["data"] == base64.b64encode(b"\x89PNG").decode()
    assert inline.get("mimeType", inline.get("mime_type")) == "image/png"
    assert parts[1] == {"text": "read"}


# ------------------------------------------------------------------------------ accounting
class MemoryLog:
    def __init__(self, budget_left: int = 100, fail_writes: bool = False) -> None:
        self.entries: list[LLMCallEntry] = []
        self.budget_left = budget_left
        self.fail_writes = fail_writes

    async def check_budget(self, provider: str) -> None:
        if self.budget_left <= len(self.entries):
            raise ProviderBudgetExceededError("budget", provider=provider)

    async def record(self, entry: LLMCallEntry) -> None:
        if self.fail_writes:
            raise RuntimeError("database down")
        self.entries.append(entry)


async def test_accounted_calls_record_tokens_cost_and_failures(
    ollama_api: respx.MockRouter,
) -> None:
    ollama_api.post("/api/chat").mock(
        side_effect=[
            httpx.Response(200, json=_chat("ok")),
            httpx.Response(400, json={"error": "x"}),
        ]
    )
    log = MemoryLog()
    llm = AccountedLLMProvider(_ollama(), log)
    request = LLMRequest(prompt="x", purpose="extraction.invoice", prompt_version="extract-v1")
    await llm.generate(request)
    with pytest.raises(ProviderRequestError):
        await llm.generate(request)
    await llm.aclose()
    ok, failed = log.entries
    assert (ok.status, ok.purpose, ok.prompt_version) == (
        LLMCallStatus.SUCCEEDED,
        "extraction.invoice",
        "extract-v1",
    )
    assert (ok.input_tokens, ok.output_tokens) == (50, 9)
    assert ok.estimated_cost_usd == Decimal(0)  # local model
    assert (failed.status, failed.error_code) == (LLMCallStatus.FAILED, "ProviderRequestError")


async def test_budget_is_checked_before_the_call(ollama_api: respx.MockRouter) -> None:
    route = ollama_api.post("/api/chat").respond(json=_chat("ok"))
    llm = AccountedLLMProvider(_ollama(), MemoryLog(budget_left=0))
    with pytest.raises(ProviderBudgetExceededError):
        await llm.generate(LLMRequest(prompt="x"))
    await llm.aclose()
    assert route.call_count == 0


async def test_accounting_failures_never_break_the_call(ollama_api: respx.MockRouter) -> None:
    ollama_api.post("/api/chat").respond(json=_chat("ok"))
    llm = AccountedLLMProvider(_ollama(), MemoryLog(fail_writes=True))
    assert (await llm.generate(LLMRequest(prompt="x"))).text == "ok"
    await llm.aclose()


def test_cost_estimate() -> None:
    usage = LLMUsage(
        "gemini", "m", 1.0, input_tokens=1_000_000, output_tokens=100_000, thinking_tokens=100_000
    )
    price = ModelPrice(Decimal("0.30"), Decimal("2.50"))
    assert estimate_cost(usage, price, local=False) == Decimal(
        "0.800000"
    )  # thinking billed as output
    assert estimate_cost(usage, None, local=False) is None  # no price configured: not estimated
    assert estimate_cost(usage, price, local=True) == Decimal(0)


def test_prices_come_from_settings_json() -> None:
    settings = make_settings(
        llm_pricing={"gemini-x": {"input_per_mtok": "0.3", "output_per_mtok": "2.5"}}
    )
    assert model_prices(settings) == {"gemini-x": ModelPrice(Decimal("0.3"), Decimal("2.5"))}


def test_gate_lets_local_models_see_restricted_content() -> None:
    external = ExternalAIGate(max_sensitivity=Sensitivity.INTERNAL, provider_configured=True)
    assert external.decide(Sensitivity.RESTRICTED).allowed is False
    local = ExternalAIGate(
        max_sensitivity=Sensitivity.INTERNAL, provider_configured=True, provider_local=True
    )
    assert local.decide(Sensitivity.RESTRICTED).allowed is True
    nothing = ExternalAIGate(
        max_sensitivity=Sensitivity.INTERNAL, provider_configured=False, provider_local=True
    )
    assert nothing.decide(Sensitivity.PUBLIC).allowed is False


def test_extraction_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="EXTRACTION_CONFIDENCE_MEDIUM"):
        make_settings(extraction_confidence_high=0.5, extraction_confidence_medium=0.7)

"""Fakes for LLM-dependent extraction tests."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel

from docintel.ai.base import LLMRequest, LLMResponse, LLMUsage, ModelTier, StructuredLLMResponse
from docintel.fields.normalize import organization_key
from docintel.fields.vendors import VendorRecord, tax_id_key

KESTREL = VendorRecord(
    uuid.uuid4(),
    "Kestrel Industrial Supply Inc.",
    organization_key("Kestrel Industrial Supply Inc."),
    (),
    tax_id_key("US-47-2917735"),
    "USD",
    30,
)


def value(text: str, quote: str, page: int = 1) -> dict[str, Any]:
    return {"value": text, "page": page, "source_text": quote}


class ScriptedLLM:
    """Returns scripted outputs (dicts) or raises scripted errors, in order."""

    def __init__(
        self, *script: dict[str, Any] | Exception, local: bool = False, images: bool = False
    ) -> None:
        self.script = list(script)
        self.requests: list[LLMRequest] = []
        self._local = local
        self._images = images

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def supports_images(self) -> bool:
        return self._images

    @property
    def local(self) -> bool:
        return self._local

    def model_for(self, tier: ModelTier) -> str:
        return f"scripted-{tier.value}"

    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredLLMResponse[T]:
        self.requests.append(request)
        step = self.script.pop(0) if self.script else {}
        if isinstance(step, Exception):
            raise step
        return StructuredLLMResponse(
            data=schema.model_validate(step),
            raw_text="{}",
            usage=LLMUsage(provider="scripted", model="scripted-default", latency_ms=3),
        )

    async def aclose(self) -> None:
        return None

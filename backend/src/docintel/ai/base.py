"""Provider-neutral AI interfaces (Module 43).

Domain code depends only on these protocols; concrete providers (Gemini now, local models in
later phases) are selected by configuration in `docintel.ai.registry`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

# All embedding providers emit 768-d L2-normalized vectors (ADR-003) so they share pgvector columns.
EMBEDDING_DIMENSIONS = 768


class ModelTier(StrEnum):
    """Callers pick a capability tier; providers map it to a concrete model id."""

    DEFAULT = "default"  # extraction, analysis, RAG answers
    FAST = "fast"  # classification fallback, query planning


class EmbeddingTask(StrEnum):
    RETRIEVAL_DOCUMENT = "RETRIEVAL_DOCUMENT"
    RETRIEVAL_QUERY = "RETRIEVAL_QUERY"
    SEMANTIC_SIMILARITY = "SEMANTIC_SIMILARITY"
    CLASSIFICATION = "CLASSIFICATION"


@dataclass(frozen=True, slots=True)
class ImageInput:
    """An image sent with a prompt (page previews for vision-capable models)."""

    data: bytes
    mime_type: str = "image/png"


@dataclass(frozen=True, slots=True)
class LLMRequest:
    prompt: str
    system_instruction: str | None = None
    tier: ModelTier = ModelTier.DEFAULT
    max_output_tokens: int | None = None
    # Free-form label for usage accounting and logs, e.g. "extraction.invoice".
    purpose: str = "general"
    # Sent only to providers that support images (`supports_images`); others ignore them.
    images: tuple[ImageInput, ...] = ()
    # Accounting context (llm_calls): never sent to the provider.
    document_id: uuid.UUID | None = None
    prompt_version: str | None = None
    agent_run_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class LLMUsage:
    provider: str
    model: str
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    usage: LLMUsage
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class StructuredLLMResponse[T: BaseModel]:
    data: T
    raw_text: str
    usage: LLMUsage


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    dimensions: int
    latency_ms: float


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def supports_images(self) -> bool: ...

    @property
    def local(self) -> bool:
        """True if content stays inside the deployment (e.g. a self-hosted model server); the
        external-AI sensitivity gate does not apply then."""
        ...

    def model_for(self, tier: ModelTier) -> str: ...

    async def generate(self, request: LLMRequest) -> LLMResponse: ...

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredLLMResponse[T]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def local(self) -> bool:
        """True if texts never leave the deployment (the sensitivity gate does not apply)."""
        ...

    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: Sequence[str], task: EmbeddingTask) -> EmbeddingResult: ...

    async def aclose(self) -> None: ...

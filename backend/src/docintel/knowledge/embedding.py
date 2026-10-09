"""Chunk and query embeddings behind the sensitivity gate (ADR-007, ADR-042).

One embedder serves knowledge ingestion, business-document indexing, re-embedding and query
time, so every vector in a column comes from the configured model and every external call is
gated the same way:

* local providers (hashing, fastembed) embed everything - nothing leaves the deployment;
* an external provider (Gemini) only receives chunks at or below AI_EXTERNAL_MAX_SENSITIVITY;
  more sensitive chunks are stored without a vector and are found by full-text search only;
* no provider configured: every chunk is full-text only, and so is every query.

A chunk without a vector is never an error: hybrid retrieval falls back to full-text ranking.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from docintel.ai.base import EMBEDDING_DIMENSIONS, EmbeddingProvider, EmbeddingTask
from docintel.ai.errors import ProviderError
from docintel.ai.registry import build_embedding_provider
from docintel.ai.routing import ExternalAIGate, GateDecision
from docintel.core.config import Settings
from docintel.core.logging import get_logger
from docintel.db.models import Sensitivity
from docintel.processing.sensitivity import assess_pages

logger = get_logger(__name__)

NOT_CONFIGURED = "no embedding provider is configured (full-text search only)"


@dataclass(frozen=True, slots=True)
class EmbeddedTexts:
    """Vectors for every text, or None with the reason (`note`) when none were made."""

    vectors: list[list[float]] | None
    model: str | None
    note: str | None = None


class ChunkEmbedder:
    def __init__(
        self,
        provider: EmbeddingProvider | None,
        *,
        max_sensitivity: Sensitivity,
        batch_size: int = 64,
    ) -> None:
        self._provider = provider
        self._batch_size = batch_size
        self._gate = ExternalAIGate(
            max_sensitivity=max_sensitivity,
            provider_configured=provider is not None,
            provider_local=provider.local if provider is not None else False,
        )

    @property
    def model(self) -> str | None:
        """Model of the vectors this embedder makes (queries only match chunks of this model)."""
        return self._provider.model if self._provider is not None else None

    @property
    def provider_name(self) -> str | None:
        return self._provider.name if self._provider is not None else None

    def decide(self, sensitivity: Sensitivity | None) -> GateDecision:
        return self._gate.decide(sensitivity)

    async def embed_documents(
        self, texts: Sequence[str], sensitivity: Sensitivity
    ) -> EmbeddedTexts:
        """Vectors for chunk texts (task RETRIEVAL_DOCUMENT). Provider errors propagate: the
        caller decides between retrying and storing the chunks without vectors."""
        if self._provider is None:
            return EmbeddedTexts(None, None, NOT_CONFIGURED)
        decision = self.decide(sensitivity)
        if not decision.allowed:
            return EmbeddedTexts(None, None, f"not embedded: {decision.reason}")
        if not texts:
            return EmbeddedTexts([], self._provider.model)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            result = await self._provider.embed(batch, EmbeddingTask.RETRIEVAL_DOCUMENT)
            if len(result.vectors) != len(batch) or result.dimensions != EMBEDDING_DIMENSIONS:
                msg = "embedding provider returned an unexpected number or size of vectors"
                raise ValueError(msg)
            vectors.extend(result.vectors)
        return EmbeddedTexts(vectors, self._provider.model)

    async def embed_query(self, text: str) -> list[float] | None:
        """Query vector, or None when dense retrieval is unavailable for this query (no
        provider, the question itself holds restricted data such as a card number, or the
        provider failed): retrieval then continues with full-text search alone.

        Only the question is sent, never document content.
        """
        if self._provider is None or not text.strip():
            return None
        detected = assess_pages([(1, text)]).detected
        if detected is not None and not self.decide(detected).allowed:
            logger.info("retrieval.query_not_embedded", reason="sensitive content in the query")
            return None
        try:
            result = await self._provider.embed([text], EmbeddingTask.RETRIEVAL_QUERY)
        except ProviderError as exc:
            logger.warning(
                "retrieval.query_embedding_failed",
                provider=self._provider.name,
                error_type=type(exc).__name__,
            )
            return None
        return result.vectors[0]

    async def aclose(self) -> None:
        if self._provider is not None:
            await self._provider.aclose()


def build_chunk_embedder(
    settings: Settings, provider: EmbeddingProvider | None = None
) -> ChunkEmbedder:
    if provider is None and settings.embedding_configured:
        provider = build_embedding_provider(settings)
    return ChunkEmbedder(
        provider,
        max_sensitivity=Sensitivity(settings.ai_external_max_sensitivity),
        batch_size=settings.embedding_batch_size,
    )

"""Local embedding providers: lexical hashing (offline) and fastembed (with a fake model)."""

from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from docintel.ai.base import EMBEDDING_DIMENSIONS, EmbeddingProvider, EmbeddingTask
from docintel.ai.errors import ProviderConfigurationError, ProviderResponseError
from docintel.ai.gemini import GeminiEmbeddingProvider
from docintel.ai.local_embeddings import (
    HASHING_MODEL,
    QUERY_INSTRUCTIONS,
    FastEmbedProvider,
    HashingEmbeddingProvider,
    lexical_tokens,
)
from docintel.ai.registry import build_embedding_provider
from tests.conftest import make_settings


def cosine(a: list[float], b: list[float]) -> float:
    return float(np.dot(a, b))


async def test_hashing_vectors_are_deterministic_and_normalized() -> None:
    provider = HashingEmbeddingProvider()
    assert isinstance(provider, EmbeddingProvider)
    result = await provider.embed(
        ["Invoices are paid on Thursday."] * 2, EmbeddingTask.RETRIEVAL_DOCUMENT
    )
    first, second = result.vectors
    assert first == second
    assert len(first) == EMBEDDING_DIMENSIONS == result.dimensions
    assert math.isclose(sum(v * v for v in first), 1.0, rel_tol=1e-9)
    assert result.model == HASHING_MODEL
    again = await HashingEmbeddingProvider().embed(
        ["Invoices are paid on Thursday."], EmbeddingTask.RETRIEVAL_QUERY
    )
    assert again.vectors[0] == first  # no state, no randomness: identical in every process


async def test_hashing_similarity_is_lexical() -> None:
    provider = HashingEmbeddingProvider()
    texts = [
        "Unit price tolerance on invoices",
        "Invoiced unit prices must match within the tolerance",
        "Hotel rates for business travel",
        "price tolerances",
        "Who approves a bill?",
    ]
    v = (await provider.embed(texts, EmbeddingTask.RETRIEVAL_DOCUMENT)).vectors
    assert cosine(v[0], v[1]) > cosine(v[0], v[2]) + 0.2
    assert cosine(v[0], v[3]) > 0.3  # character n-grams: "tolerance" ~ "tolerances"
    # Lexical only: a synonym ("bill" for invoice) shares nothing.
    assert abs(cosine(v[4], v[1])) < 0.15


async def test_hashing_rejects_texts_without_content() -> None:
    provider = HashingEmbeddingProvider()
    assert lexical_tokens("The of and, to!") == []
    with pytest.raises(ProviderResponseError):
        await provider.embed(["The of and, to!"], EmbeddingTask.RETRIEVAL_QUERY)
    with pytest.raises(ValueError, match="empty"):
        await provider.embed(["  "], EmbeddingTask.RETRIEVAL_QUERY)


class FakeModel:
    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self.dimensions = dimensions
        self.seen: list[str] = []

    def embed(self, documents: Iterable[str], batch_size: int = 32) -> Iterable[Any]:
        for document in documents:
            self.seen.append(document)
            yield np.full(self.dimensions, 2.0)


async def test_fastembed_provider_loads_once_and_prefixes_queries(tmp_path: Path) -> None:
    model = FakeModel()
    loads: list[tuple[str, str | None, int | None]] = []

    def factory(name: str, cache_dir: str | None, threads: int | None) -> FakeModel:
        loads.append((name, cache_dir, threads))
        return model

    provider = FastEmbedProvider(
        model="BAAI/bge-base-en-v1.5", cache_dir=str(tmp_path), threads=2, model_factory=factory
    )
    assert provider.local is True
    docs = await provider.embed(["Invoices are paid weekly."], EmbeddingTask.RETRIEVAL_DOCUMENT)
    query = await provider.embed(["when are invoices paid"], EmbeddingTask.RETRIEVAL_QUERY)
    assert loads == [("BAAI/bge-base-en-v1.5", str(tmp_path), 2)]
    assert model.seen == [
        "Invoices are paid weekly.",
        QUERY_INSTRUCTIONS["BAAI/bge-base-en-v1.5"] + "when are invoices paid",
    ]
    assert math.isclose(sum(v * v for v in docs.vectors[0]), 1.0)
    assert query.model == "BAAI/bge-base-en-v1.5"


async def test_fastembed_provider_checks_dimensions_and_dependency() -> None:
    wrong = FastEmbedProvider(model="m", model_factory=lambda *_: FakeModel(384))
    with pytest.raises(ProviderResponseError, match="768-d"):
        await wrong.embed(["text"], EmbeddingTask.RETRIEVAL_DOCUMENT)
    missing = FastEmbedProvider(model="BAAI/bge-base-en-v1.5")  # fastembed is an optional extra
    with pytest.raises(ProviderConfigurationError, match="local-embeddings"):
        await missing.embed(["text"], EmbeddingTask.RETRIEVAL_DOCUMENT)


def test_registry_and_configuration() -> None:
    hashing = make_settings(embedding_provider="hashing")
    assert hashing.embedding_configured
    assert isinstance(build_embedding_provider(hashing), HashingEmbeddingProvider)
    local = make_settings(embedding_provider="fastembed", fastembed_model="BAAI/bge-small-en-v1.5")
    provider = build_embedding_provider(local)
    assert isinstance(provider, FastEmbedProvider)
    assert provider.model == "BAAI/bge-small-en-v1.5"
    assert not make_settings(embedding_provider="gemini", gemini_api_key=None).embedding_configured
    assert GeminiEmbeddingProvider.local is False  # external: the sensitivity gate applies

"""Embedding providers that run inside the deployment (no content leaves it).

* `HashingEmbeddingProvider` — deterministic **lexical** vectors: words, word pairs and character
  n-grams, signed-hashed into 768 dimensions (feature hashing). No model download, no network,
  identical results everywhere, so the whole retrieval stack (pgvector, hybrid fusion, access
  filters) runs in tests, CI and offline installs. It knows nothing about meaning: "invoice" and
  "bill" are unrelated to it. Use Gemini or fastembed for semantic retrieval.
* `FastEmbedProvider` — a real sentence-embedding model (default `BAAI/bge-base-en-v1.5`, 768-d)
  run with ONNX Runtime through fastembed. Optional dependency (`uv sync --extra
  local-embeddings`); the model is downloaded from Hugging Face on first use into
  `FASTEMBED_CACHE_DIR`.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import itertools
import math
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Protocol

import numpy as np

from docintel.ai.base import EMBEDDING_DIMENSIONS, EmbeddingResult, EmbeddingTask
from docintel.ai.errors import ProviderConfigurationError, ProviderResponseError
from docintel.core.logging import get_logger

logger = get_logger(__name__)

HASHING_MODEL = "hashing-ngram-v1"
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
# Function words carry no topic; without corpus statistics (IDF) they would dominate the vector.
# fmt: off
_STOPWORDS = frozenset((
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "could", "do", "does",
    "for", "from", "had", "has", "have", "he", "her", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "may", "might", "must", "no", "not", "of", "on", "or", "our", "shall", "she",
    "should", "so", "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "those", "to", "under", "up", "us", "was", "we", "were", "what", "when", "where",
    "which", "who", "whom", "why", "will", "with", "within", "without", "would", "you", "your",
))
# fmt: on
# Words decide; word pairs add phrases; character n-grams add morphology ("tolerance" ~
# "tolerances") with a weight small enough that the many n-grams of a word do not outvote it.
_KIND_WEIGHTS = {"w": 1.0, "p": 0.5, "c": 0.12}
_GRAM_SIZES = (3, 4, 5)


def _normalize(vector: np.ndarray[Any, np.dtype[np.float64]]) -> list[float]:
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        msg = "cannot normalize a zero vector"
        raise ValueError(msg)
    return [float(value) for value in vector / norm]


def lexical_tokens(text: str) -> list[str]:
    """Case-folded word tokens without stopwords (NFKC: ligatures, full-width forms)."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return [token for token in _TOKEN.findall(folded) if token not in _STOPWORDS]


def _bucket(feature: str, dimensions: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dimensions, (1.0 if value >> 63 else -1.0)


class HashingEmbeddingProvider:
    """Signed feature hashing of words, word pairs and character n-grams (lexical, not semantic)."""

    name = "hashing"
    local = True

    def __init__(self, *, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self._dimensions = dimensions

    @property
    def model(self) -> str:
        return HASHING_MODEL

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def features(self, text: str) -> dict[str, float]:
        """Weighted features of a text: kind weight x sublinear term frequency (1 + log tf)."""
        tokens = lexical_tokens(text)
        counts: Counter[str] = Counter()
        for token in tokens:
            counts[f"w:{token}"] += 1
            padded = f"<{token}>"
            for size in _GRAM_SIZES:
                counts.update(f"c:{padded[i : i + size]}" for i in range(len(padded) - size + 1))
        counts.update(f"p:{a} {b}" for a, b in itertools.pairwise(tokens))
        return {
            feature: _KIND_WEIGHTS[feature[0]] * (1.0 + math.log(tf))
            for feature, tf in counts.items()
        }

    def vector(self, text: str) -> list[float]:
        dense = np.zeros(self._dimensions)
        for feature, weight in self.features(text).items():
            index, sign = _bucket(feature, self._dimensions)
            dense[index] += sign * weight
        return _normalize(dense)

    async def embed(self, texts: Sequence[str], task: EmbeddingTask) -> EmbeddingResult:
        if any(not text.strip() for text in texts):
            msg = "cannot embed empty or whitespace-only text"
            raise ValueError(msg)
        started = time.perf_counter()
        try:
            vectors = [self.vector(text) for text in texts]
        except ValueError as exc:  # only stopwords and punctuation: nothing to represent
            raise ProviderResponseError(str(exc), provider=self.name) from exc
        return EmbeddingResult(
            vectors=vectors,
            model=HASHING_MODEL,
            dimensions=self._dimensions,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def aclose(self) -> None:
        return None


class _TextEmbeddingModel(Protocol):
    """The part of `fastembed.TextEmbedding` this provider uses."""

    def embed(self, documents: Iterable[str], batch_size: int = ...) -> Iterable[Any]: ...


ModelFactory = Callable[[str, str | None, int | None], _TextEmbeddingModel]

# BGE v1.5 retrieves best when short queries carry this instruction (passages do not).
QUERY_INSTRUCTIONS = {
    "BAAI/bge-base-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-small-en-v1.5": "Represent this sentence for searching relevant passages: ",
}


def _fastembed_model(model: str, cache_dir: str | None, threads: int | None) -> Any:
    try:
        module = importlib.import_module("fastembed")
    except ImportError as exc:
        msg = (
            "EMBEDDING_PROVIDER=fastembed needs the optional dependency: "
            "uv sync --extra local-embeddings"
        )
        raise ProviderConfigurationError(msg, provider="fastembed") from exc
    return module.TextEmbedding(model_name=model, cache_dir=cache_dir, threads=threads)


class FastEmbedProvider:
    """A local sentence-embedding model through fastembed (ONNX Runtime, CPU)."""

    name = "fastembed"
    local = True

    def __init__(
        self,
        *,
        model: str,
        cache_dir: str | None = None,
        threads: int | None = None,
        batch_size: int = 32,
        dimensions: int = EMBEDDING_DIMENSIONS,
        model_factory: ModelFactory = _fastembed_model,
    ) -> None:
        self._model_name = model
        self._cache_dir = cache_dir
        self._threads = threads
        self._batch_size = batch_size
        self._dimensions = dimensions
        self._factory = model_factory
        self._model: _TextEmbeddingModel | None = None
        self._lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def _loaded(self) -> _TextEmbeddingModel:
        async with self._lock:  # load (and download) once, off the event loop
            if self._model is None:
                self._model = await asyncio.to_thread(
                    self._factory, self._model_name, self._cache_dir, self._threads
                )
            return self._model

    async def embed(self, texts: Sequence[str], task: EmbeddingTask) -> EmbeddingResult:
        if any(not text.strip() for text in texts):
            msg = "cannot embed empty or whitespace-only text"
            raise ValueError(msg)
        started = time.perf_counter()
        model = await self._loaded()
        prefix = QUERY_INSTRUCTIONS.get(self._model_name, "")
        query = task == EmbeddingTask.RETRIEVAL_QUERY
        inputs = [prefix + text if query else text for text in texts]

        def run() -> list[list[float]]:
            vectors: list[list[float]] = []
            for raw in model.embed(inputs, batch_size=self._batch_size):
                values = np.asarray(raw, dtype=np.float64)
                if values.shape != (self._dimensions,):
                    msg = f"expected {self._dimensions}-d embedding, got shape {values.shape}"
                    raise ProviderResponseError(msg, provider=self.name)
                vectors.append(_normalize(values))
            return vectors

        vectors = await asyncio.to_thread(run)
        if len(vectors) != len(texts):
            msg = f"fastembed returned {len(vectors)} embeddings for {len(texts)} inputs"
            raise ProviderResponseError(msg, provider=self.name)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "ai.embedding.call",
            provider=self.name,
            model=self._model_name,
            task=task.value,
            count=len(texts),
            latency_ms=latency_ms,
        )
        return EmbeddingResult(
            vectors=vectors,
            model=self._model_name,
            dimensions=self._dimensions,
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        self._model = None

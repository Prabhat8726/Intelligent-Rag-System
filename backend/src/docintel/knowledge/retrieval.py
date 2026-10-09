"""Hybrid retrieval over the knowledge base (Module 13, docs/architecture/07 section 2).

  question -> dense: pgvector cosine (HNSW, iterative scan) over chunks of the current model
           -> full text: OR of the question's stemmed terms (GIN index), ordered by
              ts_rank_cd - or, when there is no dense ranking to fuse with, by the
              rarity-weighted share of the question's terms a passage contains
           -> Reciprocal Rank Fusion (k=60) -> top-k passages -> evidence gate

Every filter is SQL inside both scans, so passages the user may not read, versions not in
force on the requested date and other categories never leave the database:

* access: organization-wide chunks plus the user's department (administrators: all);
* versions: ACTIVE and SUPERSEDED chunks whose retrieval window contains `as_of` (default
  today; see knowledge/lifecycle.py), so superseded policy is cited only for past dates;
* optional categories and document keys.

Evidence gate: the best of the top five passages must contain enough of the question's terms,
weighted by rarity (IDF over the passages in scope), or be close enough in embedding space.
Below both thresholds the platform says "insufficient evidence" instead of asking a model.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import ColumnElement, func, literal, or_, select, text, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from docintel.auth.policies import visible_knowledge_chunks
from docintel.db.models import (
    KnowledgeCategory,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeStatus,
    Sensitivity,
    User,
)
from docintel.knowledge.embedding import ChunkEmbedder
from docintel.knowledge.lifecycle import RETRIEVABLE_STATUSES

COVERAGE_DEPTH = 5  # passages considered by the evidence gate


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    candidates: int = 20
    top_k: int = 6
    rrf_k: int = 60
    min_term_coverage: float = 0.25
    min_dense_similarity: float = 0.5
    # Order of full-text candidates: "idf" = share of the question's rare terms a passage
    # contains (ties: ts_rank_cd), "ts_rank" = PostgreSQL cover density alone, "auto" = idf when
    # full text is the only retriever, ts_rank when it is fused with dense results (the better
    # of the two for each mode on the kb-queries tuning set; see evaluation/reports/retrieval.md).
    text_ranking: str = "auto"
    use_dense: bool = True  # switches for evaluation ablations
    use_full_text: bool = True


@dataclass(frozen=True, slots=True)
class KnowledgeScope:
    as_of: date
    categories: tuple[KnowledgeCategory, ...] = ()
    document_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Passage:
    chunk_id: uuid.UUID
    knowledge_document_id: uuid.UUID
    chunk_index: int
    document_key: str
    title: str
    version_label: str | None
    category: KnowledgeCategory
    status: KnowledgeStatus
    section_path: str
    heading: str
    content: str
    page_start: int | None
    page_end: int | None
    effective_from: date | None
    effective_to: date | None
    sensitivity: Sensitivity
    score: float
    dense_rank: int | None
    dense_similarity: float | None
    text_rank: int | None
    text_score: float | None
    term_coverage: float


@dataclass(frozen=True, slots=True)
class Evidence:
    sufficient: bool
    term_coverage: float
    dense_similarity: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class Retrieval:
    passages: list[Passage]
    evidence: Evidence
    mode: str  # "hybrid", "full_text", "dense" or "none"
    embedding_model: str | None
    as_of: date
    query_terms: list[str]
    timings_ms: dict[str, float] = field(default_factory=dict)


# ------------------------------------------------------------------------------ pure parts
def rrf_fuse(rankings: Sequence[Sequence[uuid.UUID]], k: int) -> list[tuple[uuid.UUID, float]]:
    """Reciprocal Rank Fusion: score = sum over rankings of 1 / (k + rank). Ties (e.g. ranks
    1 and 2 against 2 and 1) go to the better single rank, then to the earlier ranking."""
    scores: dict[uuid.UUID, float] = {}
    ranks: dict[uuid.UUID, list[float]] = {}
    for position, ranking in enumerate(rankings):
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(item, [math.inf] * len(rankings))[position] = rank
    return sorted(scores.items(), key=lambda pair: (-pair[1], min(ranks[pair[0]]), ranks[pair[0]]))


def term_weights(document_frequency: Mapping[str, int], total: int) -> dict[str, float]:
    """IDF weight of each query term over the passages in scope (rare terms count more)."""
    return {
        term: math.log((total + 1) / (frequency + 0.5))
        for term, frequency in document_frequency.items()
    }


def term_coverage(weights: Mapping[str, float], lexemes: Iterable[str]) -> float:
    """Share of the question's (weighted) terms that occur in a passage."""
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    present = set(lexemes)
    return sum(weight for term, weight in weights.items() if term in present) / total


def assess_evidence(
    coverage: float, dense_similarity: float | None, options: RetrievalOptions, found: bool
) -> Evidence:
    if not found:
        return Evidence(False, 0.0, dense_similarity, "no passage matches the question")
    if coverage >= options.min_term_coverage:
        return Evidence(
            True, coverage, dense_similarity, "the passages contain the question's terms"
        )
    if dense_similarity is not None and dense_similarity >= options.min_dense_similarity:
        return Evidence(True, coverage, dense_similarity, "the passages are semantically close")
    return Evidence(
        False,
        coverage,
        dense_similarity,
        "the closest passages do not cover the question (insufficient evidence)",
    )


def tsquery_literal(term: str) -> str:
    """A single quoted tsquery operand (no operators can be injected through a term)."""
    return "'" + term.replace("\\", "\\\\").replace("'", "''") + "'"


# ------------------------------------------------------------------------------ retriever
class KnowledgeRetriever:
    def __init__(
        self,
        session: AsyncSession,
        embedder: ChunkEmbedder | None,
        options: RetrievalOptions | None = None,
    ) -> None:
        self._session = session
        self._embedder = embedder
        self._options = options or RetrievalOptions()

    def _scope(self, user: User, scope: KnowledgeScope) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [
            visible_knowledge_chunks(user),
            KnowledgeChunk.status.in_(RETRIEVABLE_STATUSES),
            or_(
                KnowledgeChunk.effective_from.is_(None),
                KnowledgeChunk.effective_from <= scope.as_of,
            ),
            or_(KnowledgeChunk.effective_to.is_(None), KnowledgeChunk.effective_to >= scope.as_of),
        ]
        if scope.categories:
            conditions.append(KnowledgeChunk.category.in_(scope.categories))
        if scope.document_keys:
            conditions.append(
                KnowledgeChunk.knowledge_document_id.in_(
                    select(KnowledgeDocument.id).where(
                        KnowledgeDocument.document_key.in_(scope.document_keys)
                    )
                )
            )
        return conditions

    async def query_terms(self, question: str) -> list[str]:
        terms = await self._session.scalars(
            text("SELECT lexeme FROM unnest(to_tsvector('english', :q)) ORDER BY lexeme"),
            {"q": question},
        )
        return list(terms)

    async def _dense(
        self, vector: list[float], model: str, conditions: list[ColumnElement[bool]]
    ) -> list[tuple[uuid.UUID, float]]:
        options = self._options
        # pgvector 0.8: keep scanning the HNSW graph until enough rows pass the filters.
        await self._session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        ef_search = max(40, min(1000, options.candidates * 2))
        await self._session.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search:d}"))
        distance = KnowledgeChunk.embedding.cosine_distance(vector)
        rows = await self._session.execute(
            select(KnowledgeChunk.id, KnowledgeChunk.content_hash, distance.label("distance"))
            .where(
                *conditions,
                KnowledgeChunk.embedding.is_not(None),
                KnowledgeChunk.embedding_model == model,
            )
            .order_by(distance, KnowledgeChunk.content_hash)
            .limit(options.candidates)
        )
        # relaxed_order may return rows slightly out of order: sort exactly (ties by content,
        # so equal inputs always give the same order).
        ordered = sorted(rows, key=lambda row: (float(row.distance), row.content_hash))
        return [(row.id, 1.0 - float(row.distance)) for row in ordered]

    async def _full_text(
        self,
        terms: list[str],
        conditions: list[ColumnElement[bool]],
        weights: Mapping[str, float],
        ranking: str,
    ) -> list[tuple[uuid.UUID, float]]:
        if not terms:
            return []
        query = func.to_tsquery("simple", literal(" | ".join(map(tsquery_literal, terms))))
        rank = func.ts_rank_cd(KnowledgeChunk.search, query, 32)
        rows = (
            await self._session.execute(
                select(
                    KnowledgeChunk.id,
                    KnowledgeChunk.content_hash,
                    rank.label("rank"),
                    func.tsvector_to_array(KnowledgeChunk.search).label("lexemes"),
                )
                .where(*conditions, KnowledgeChunk.search.op("@@")(query))
                .order_by(rank.desc(), KnowledgeChunk.content_hash)
                .limit(self._options.candidates)
            )
        ).all()
        if ranking == "idf":
            # ts_rank_cd ignores how rare a term is: "tolerance" should outweigh "price".
            rows = sorted(
                rows,
                key=lambda row: (
                    -term_coverage(weights, row.lexemes or ()),
                    -float(row.rank),
                    row.content_hash,
                ),
            )
        return [(row.id, float(row.rank)) for row in rows]

    async def _document_frequency(
        self, terms: list[str], conditions: list[ColumnElement[bool]]
    ) -> tuple[dict[str, int], int]:
        total = await self._session.scalar(
            select(func.count()).select_from(KnowledgeChunk).where(*conditions)
        )
        if not terms:
            return {}, int(total or 0)
        counts = union_all(
            *(
                select(literal(index).label("term"), func.count().label("df"))
                .select_from(KnowledgeChunk)
                .where(
                    *conditions,
                    KnowledgeChunk.search.op("@@")(
                        func.to_tsquery("simple", literal(tsquery_literal(term)))
                    ),
                )
                for index, term in enumerate(terms)
            )
        )
        rows = await self._session.execute(counts)
        frequency = {terms[row.term]: int(row.df) for row in rows}
        return frequency, int(total or 0)

    async def retrieve(self, user: User, question: str, scope: KnowledgeScope) -> Retrieval:
        options = self._options
        timings: dict[str, float] = {}
        started = time.perf_counter()
        conditions = self._scope(user, scope)
        terms = await self.query_terms(question)

        dense: list[tuple[uuid.UUID, float]] = []
        model = self._embedder.model if self._embedder is not None else None
        if options.use_dense and self._embedder is not None and model is not None:
            step = time.perf_counter()
            vector = await self._embedder.embed_query(question)
            timings["embed_query"] = _ms(step)
            if vector is not None:
                step = time.perf_counter()
                dense = await self._dense(vector, model, conditions)
                timings["dense"] = _ms(step)
        step = time.perf_counter()
        frequency, total = await self._document_frequency(terms, conditions)
        weights = term_weights(frequency, total)
        timings["term_statistics"] = _ms(step)
        lexical: list[tuple[uuid.UUID, float]] = []
        if options.use_full_text:
            step = time.perf_counter()
            ranking = options.text_ranking
            if ranking == "auto":
                ranking = "ts_rank" if dense else "idf"
            lexical = await self._full_text(terms, conditions, weights, ranking)
            timings["full_text"] = _ms(step)

        rankings = [[chunk for chunk, _ in ranking] for ranking in (dense, lexical) if ranking]
        fused = rrf_fuse(rankings, options.rrf_k)[: options.top_k]
        dense_info = {chunk: (rank, score) for rank, (chunk, score) in enumerate(dense, 1)}
        text_info = {chunk: (rank, score) for rank, (chunk, score) in enumerate(lexical, 1)}
        passages = await self._load(fused, dense_info, text_info, weights)

        coverage = max((p.term_coverage for p in passages[:COVERAGE_DEPTH]), default=0.0)
        top_similarity = dense[0][1] if dense else None
        evidence = assess_evidence(coverage, top_similarity, options, bool(passages))
        mode = "hybrid" if dense and options.use_full_text else "dense" if dense else "full_text"
        if not options.use_full_text and not dense:
            mode = "none"
        timings["total"] = _ms(started)
        return Retrieval(
            passages=passages,
            evidence=evidence,
            mode=mode,
            embedding_model=model if dense else None,
            as_of=scope.as_of,
            query_terms=terms,
            timings_ms=timings,
        )

    async def _load(
        self,
        fused: list[tuple[uuid.UUID, float]],
        dense_info: dict[uuid.UUID, tuple[int, float]],
        text_info: dict[uuid.UUID, tuple[int, float]],
        weights: Mapping[str, float],
    ) -> list[Passage]:
        if not fused:
            return []
        ids = [chunk for chunk, _ in fused]
        rows = await self._session.execute(
            select(
                KnowledgeChunk,
                KnowledgeDocument.document_key,
                KnowledgeDocument.title,
                KnowledgeDocument.version_label,
                func.tsvector_to_array(KnowledgeChunk.search).label("lexemes"),
            )
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.knowledge_document_id)
            .where(KnowledgeChunk.id.in_(ids))
            .options(defer(KnowledgeChunk.embedding), defer(KnowledgeChunk.search))
        )
        by_id = {row.KnowledgeChunk.id: row for row in rows}
        passages: list[Passage] = []
        for chunk_id, score in fused:
            row = by_id.get(chunk_id)
            if row is None:
                continue
            chunk: KnowledgeChunk = row.KnowledgeChunk
            dense_rank, similarity = dense_info.get(chunk_id, (None, None))
            text_rank, text_score = text_info.get(chunk_id, (None, None))
            passages.append(
                Passage(
                    chunk_id=chunk.id,
                    knowledge_document_id=chunk.knowledge_document_id,
                    chunk_index=chunk.chunk_index,
                    document_key=row.document_key,
                    title=row.title,
                    version_label=row.version_label,
                    category=chunk.category,
                    status=chunk.status,
                    section_path=chunk.section_path,
                    heading=chunk.heading,
                    content=chunk.content,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    effective_from=chunk.effective_from,
                    effective_to=chunk.effective_to,
                    sensitivity=chunk.sensitivity,
                    score=score,
                    dense_rank=dense_rank,
                    dense_similarity=similarity,
                    text_rank=text_rank,
                    text_score=text_score,
                    term_coverage=term_coverage(weights, row.lexemes or ()),
                )
            )
        return passages


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)

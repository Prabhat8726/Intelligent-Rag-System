"""Retrieval suite: the knowledge base search behind RAG answers (Modules 12, 13, 25).

The seed knowledge base (knowledge_base/*.md) is ingested into a scratch PostgreSQL database
exactly as in production: KnowledgeService.upload, then the worker's KNOWLEDGE_PROCESSING job
(section-aware chunking, full-text vectors, embeddings). Questions are answered by
KnowledgeRetriever, the code behind POST /knowledge/search and /knowledge/query.

Embeddings: the offline lexical hashing model (`hashing-ngram-v1`). It is NOT semantic: it
matches words and word fragments, not meaning. Gemini and fastembed embeddings were not
measured (no API key / model download in the environment that produced the report).

Datasets: evaluation/datasets/kb-queries.json (54 questions; the evidence-gate thresholds and
the full-text ordering were chosen on it, so its numbers are optimistic) and
kb-queries-holdout.json (14 questions written afterwards, never used for tuning).

Measured: hit@k (a relevant passage in the first k), section recall@5, precision@5, MRR,
nDCG@5; the evidence gate (refusal of unanswerable questions, false refusals); access control
(passages of another department's documents returned to a Finance user); version filtering;
latency. Ablations: dense vs full text vs hybrid, full-text ordering, contextual prefix off,
fixed-size chunks instead of section-aware chunks.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
from fastapi import UploadFile
from psycopg import sql
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.datastructures import Headers

from docintel.ai.base import EmbeddingTask
from docintel.ai.local_embeddings import HASHING_MODEL, HashingEmbeddingProvider
from docintel.audit.service import SYSTEM_REQUEST
from docintel.core.config import Settings
from docintel.db import migrations_runner
from docintel.db.models import (
    Department,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeStatus,
    Role,
    Sensitivity,
    User,
)
from docintel.evaluation.metrics import (
    hit_at_k,
    ndcg_at_k,
    precision_at_k,
    reciprocal_rank,
    summary,
)
from docintel.evaluation.report import Report, environment, num, pct
from docintel.knowledge.chunking import PATH_SEPARATOR, chunk_source, estimate_tokens
from docintel.knowledge.embedding import ChunkEmbedder
from docintel.knowledge.retrieval import (
    KnowledgeRetriever,
    KnowledgeScope,
    Passage,
    Retrieval,
    RetrievalOptions,
)
from docintel.knowledge.service import KnowledgeService
from docintel.knowledge.sources import UnitKind, parse_markdown, split_front_matter
from docintel.knowledge.validation import MetadataInput
from docintel.processing.services import build_processing_services
from docintel.storage import LocalStorage
from docintel.workers.runner import Worker

ROOT = Path(__file__).resolve().parents[4]
KNOWLEDGE_BASE = ROOT / "knowledge_base"
DATASETS = ROOT / "evaluation" / "datasets"
EVAL_DATE = date(2026, 10, 1)  # fixed "today": results do not drift with the calendar
PAST_DATE = date(2025, 6, 30)
DEPTH = 10  # passages retrieved per question for the metrics
FIXED_CHARS = 2000  # fixed-size baseline: ~500 tokens, like the structure-aware target
FIXED_OVERLAP = 300
_HEADING_MARK = re.compile(r"^#{1,6}\s+")


@dataclass(frozen=True, slots=True)
class Config:
    name: str
    corpus: str  # "sections", "no_prefix" or "fixed"
    options: RetrievalOptions


@dataclass(slots=True)
class QueryResult:
    query: dict[str, Any]
    labels: list[list[tuple[str, str]]]  # per passage: the labelled sections it belongs to
    retrieval: Retrieval

    @property
    def relevant(self) -> list[bool]:
        return [bool(found) for found in self.labels]

    def section_recall(self, k: int) -> float:
        found = {label for labels in self.labels[:k] for label in labels}
        return len(found) / len(self.query["relevant"])


# ------------------------------------------------------------------------------ database
@asynccontextmanager
async def scratch_database(base_url: str) -> AsyncIterator[str]:
    """A uniquely named, migrated database on the server of `base_url`; dropped afterwards."""
    name = f"docintel_eval_{uuid.uuid4().hex[:12]}"
    server = make_url(base_url).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_url(base_url).set(database=name).render_as_string(hide_password=False)
    try:
        migrations_runner.upgrade(url)
        yield url
    finally:
        with psycopg.connect(server, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _settings(database_url: str, storage_root: Path) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": database_url,
        "jwt_secret_key": secrets.token_urlsafe(48),
        "storage_local_root": storage_root,
        "embedding_provider": "hashing",
        "gemini_api_key": None,
        "seed_user_password": None,
        "log_level": "WARNING",
    }
    return Settings(_env_file=None, **values)


async def _users(session: AsyncSession) -> dict[str, User]:
    finance = Department(id=uuid.uuid4(), name="Finance")
    legal = Department(id=uuid.uuid4(), name="Legal")
    users = {
        "admin": User(role=Role.ADMIN, department_id=None),
        "finance": User(role=Role.VIEWER, department_id=finance.id),
        "legal": User(role=Role.REVIEWER, department_id=legal.id),
    }
    for name, user in users.items():
        user.id = uuid.uuid4()
        user.email = f"{name}@eval.invalid"
        user.full_name = f"Evaluation {name}"
        user.password_hash = "not-used"  # noqa: S105  (no login in the evaluation)
    session.add_all([finance, legal, *users.values()])
    await session.commit()
    return users


async def _ingest(
    maker: async_sessionmaker[AsyncSession], settings: Settings, admin: User, storage: LocalStorage
) -> None:
    for path in sorted(KNOWLEDGE_BASE.glob("*.md")):
        upload = UploadFile(
            io.BytesIO(path.read_bytes()),
            filename=path.name,
            headers=Headers({"content-type": "text/markdown"}),
        )
        async with maker() as session:
            await KnowledgeService(session, storage, settings).upload(
                actor=admin,
                upload=upload,
                form=MetadataInput(),
                department_id=None,
                meta=SYSTEM_REQUEST,
            )
    embedder = ChunkEmbedder(HashingEmbeddingProvider(), max_sensitivity=Sensitivity.INTERNAL)
    services = build_processing_services(settings, embedder=embedder)
    await Worker(
        settings=settings, sessionmaker=maker, storage=storage, services=services
    ).run_until_idle()


# ------------------------------------------------------------------------------ ablation corpora
def fixed_size_chunks(body: str) -> list[str]:
    """Baseline: the document body cut into ~FIXED_CHARS windows at whitespace, ignoring
    sections and tables (heading markers removed)."""
    text = "\n".join(_HEADING_MARK.sub("", line) for line in body.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + FIXED_CHARS)
        if end < len(text):
            space = text.rfind(" ", start + FIXED_CHARS // 2, end)
            end = space if space > start else end
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - FIXED_OVERLAP, start + 1)
        while start < len(text) and not text[start - 1].isspace():
            start += 1  # begin at a word boundary
    return [chunk for chunk in chunks if chunk]


async def _clone_corpus(
    maker: async_sessionmaker[AsyncSession], corpus: str, sources: dict[tuple[str, str], str]
) -> None:
    """Copies of the ingested documents (same metadata, status and dates) with chunks made
    differently: without the title/breadcrumb prefix, or as fixed-size windows."""
    provider = HashingEmbeddingProvider()
    async with maker() as session:
        documents = (
            await session.scalars(
                select(KnowledgeDocument).where(KnowledgeDocument.document_key.not_like("%~%"))
            )
        ).all()
        for document in documents:
            window = (
                await session.execute(
                    select(KnowledgeChunk.effective_from, KnowledgeChunk.effective_to)
                    .where(KnowledgeChunk.knowledge_document_id == document.id)
                    .limit(1)
                )
            ).first()
            text = sources[document.document_key, document.version_label or ""]
            copy = KnowledgeDocument(
                id=uuid.uuid4(),
                document_key=f"{document.document_key}~{corpus}",
                title=document.title,
                category=document.category,
                version_label=document.version_label,
                department_id=document.department_id,
                sensitivity=document.sensitivity,
                effective_sensitivity=document.effective_sensitivity,
                effective_from=document.effective_from,
                effective_to=document.effective_to,
                status=document.status,
                source_format=document.source_format,
                original_filename=document.original_filename,
                mime_type=document.mime_type,
                size_bytes=document.size_bytes,
                sha256=document.sha256,
                storage_backend=document.storage_backend,
                storage_key=document.storage_key,
                uploaded_by_id=document.uploaded_by_id,
                processed_at=document.processed_at,
            )
            session.add(copy)
            await session.flush()  # the chunks reference it
            rows: list[tuple[str, str, str, str]] = []  # (prefix, content, path, heading)
            if corpus == "no_prefix":
                for chunk in chunk_source(parse_markdown(text), title=document.title):
                    rows.append(("", chunk.content, chunk.breadcrumb, chunk.heading))
            else:
                _, body = split_front_matter(text)
                for content in fixed_size_chunks(body):
                    rows.append((document.title, content, "", document.title))
            texts = [
                f"{prefix}\n\n{content}" if prefix else content for prefix, content, _, _ in rows
            ]
            vectors = (await provider.embed(texts, EmbeddingTask.RETRIEVAL_DOCUMENT)).vectors
            for index, ((prefix, content, path, heading), vector) in enumerate(
                zip(rows, vectors, strict=True)
            ):
                session.add(
                    KnowledgeChunk(
                        knowledge_document_id=copy.id,
                        chunk_index=index,
                        context_prefix=prefix,
                        content=content,
                        section_path=path,
                        heading=heading[:300],
                        kind="text",
                        token_count=estimate_tokens(content),
                        content_hash=hashlib.sha256(f"{prefix}\n\n{content}".encode()).hexdigest(),
                        embedding=vector,
                        embedding_model=HASHING_MODEL,
                        status=document.status,
                        department_id=document.department_id,
                        category=document.category,
                        sensitivity=document.effective_sensitivity or document.sensitivity,
                        effective_from=window.effective_from if window else None,
                        effective_to=window.effective_to if window else None,
                    )
                )
        await session.commit()


# ------------------------------------------------------------------------------ relevance
def section_markers(sources: dict[tuple[str, str], str]) -> dict[tuple[str, str], list[str]]:
    """(document_key, section heading) -> texts that identify the section inside a fixed-size
    chunk: its heading and the start of its first paragraph."""
    markers: dict[tuple[str, str], list[str]] = {}
    for (key, _), text in sources.items():
        units = parse_markdown(text).units
        for index, unit in enumerate(units):
            if unit.kind != UnitKind.HEADING:
                continue
            found = markers.setdefault((key, unit.text), [unit.text])
            following = next((u for u in units[index + 1 :] if u.kind != UnitKind.HEADING), None)
            if following is not None:
                found.append(following.text[:80])
    return markers


def _base_key(key: str) -> str:
    return key.split("~", 1)[0]


def is_relevant(
    passage: Passage, query: dict[str, Any], corpus: str, markers: dict[tuple[str, str], list[str]]
) -> list[tuple[str, str]]:
    """The labelled (document, section) pairs of `query` this passage belongs to."""
    key = _base_key(passage.document_key)
    hits = []
    for label in query["relevant"]:
        if label["document"] != key:
            continue
        if corpus == "fixed":
            texts = markers.get((key, label["section"]), [label["section"]])
            if any(text and text in passage.content for text in texts):
                hits.append((key, label["section"]))
        elif label["section"] in passage.section_path.split(PATH_SEPARATOR):
            hits.append((key, label["section"]))
    return hits


async def _relevant_counts(
    maker: async_sessionmaker[AsyncSession],
    queries: Sequence[dict[str, Any]],
    corpus: str,
    keys: Sequence[str],
    markers: dict[tuple[str, str], list[str]],
) -> dict[str, int]:
    """Relevant passages per question in the corpus (in force on EVAL_DATE): the ideal for
    nDCG."""
    async with maker() as session:
        rows = (
            await session.execute(
                select(KnowledgeChunk, KnowledgeDocument.document_key)
                .join(KnowledgeDocument)
                .where(
                    KnowledgeDocument.document_key.in_(keys),
                    KnowledgeChunk.status.in_((KnowledgeStatus.ACTIVE, KnowledgeStatus.SUPERSEDED)),
                )
            )
        ).all()
    counts: dict[str, int] = {}
    for query in queries:
        total = 0
        for chunk, key in rows:
            if chunk.effective_from and chunk.effective_from > EVAL_DATE:
                continue
            if chunk.effective_to and chunk.effective_to < EVAL_DATE:
                continue
            passage = _as_passage(chunk, key)
            if is_relevant(passage, query, corpus, markers):
                total += 1
        counts[query["id"]] = total
    return counts


def _as_passage(chunk: KnowledgeChunk, key: str) -> Passage:
    return Passage(
        chunk_id=chunk.id,
        knowledge_document_id=chunk.knowledge_document_id,
        chunk_index=chunk.chunk_index,
        document_key=key,
        title="",
        version_label=None,
        category=chunk.category,
        status=chunk.status,
        section_path=chunk.section_path,
        heading=chunk.heading,
        content=chunk.content,
        page_start=None,
        page_end=None,
        effective_from=chunk.effective_from,
        effective_to=chunk.effective_to,
        sensitivity=chunk.sensitivity,
        score=0.0,
        dense_rank=None,
        dense_similarity=None,
        text_rank=None,
        text_score=None,
        term_coverage=0.0,
    )


# ------------------------------------------------------------------------------ runs
async def _run(
    maker: async_sessionmaker[AsyncSession],
    embedder: ChunkEmbedder,
    user: User,
    queries: Sequence[dict[str, Any]],
    config: Config,
    keys: Sequence[str],
    markers: dict[tuple[str, str], list[str]],
    as_of: date = EVAL_DATE,
) -> list[QueryResult]:
    results: list[QueryResult] = []
    async with maker() as session:
        retriever = KnowledgeRetriever(session, embedder, config.options)
        scope = KnowledgeScope(as_of=as_of, document_keys=tuple(keys))
        for query in queries:
            retrieval = await retriever.retrieve(user, query["question"], scope)
            labels = [is_relevant(p, query, config.corpus, markers) for p in retrieval.passages]
            results.append(QueryResult(query, labels, retrieval))
        await session.rollback()
    return results


def ranking_metrics(results: Iterable[QueryResult], ideal: dict[str, int]) -> dict[str, Any]:
    answerable = [r for r in results if r.query["relevant"]]
    if not answerable:
        return {}
    n = len(answerable)

    def mean(values: Iterable[float]) -> float:
        return round(sum(values) / n, 4)

    return {
        "questions": n,
        "hit@1": mean(hit_at_k(r.relevant, 1) for r in answerable),
        "hit@3": mean(hit_at_k(r.relevant, 3) for r in answerable),
        "hit@5": mean(hit_at_k(r.relevant, 5) for r in answerable),
        "section_recall@5": mean(r.section_recall(5) for r in answerable),
        "precision@5": mean(precision_at_k(r.relevant, 5) for r in answerable),
        "mrr": mean(reciprocal_rank(r.relevant) for r in answerable),
        "ndcg@5": mean(ndcg_at_k(r.relevant, ideal.get(r.query["id"], 0), 5) for r in answerable),
    }


def gate_metrics(results: Sequence[QueryResult]) -> dict[str, Any]:
    answerable = [r for r in results if r.query["relevant"]]
    unanswerable = [r for r in results if not r.query["relevant"]]
    refused = [r for r in unanswerable if not r.retrieval.evidence.sufficient]
    false_refusals = [r for r in answerable if not r.retrieval.evidence.sufficient]
    return {
        "unanswerable": len(unanswerable),
        "correctly_refused": len(refused),
        "refusal_rate": round(len(refused) / len(unanswerable), 4) if unanswerable else None,
        "answerable": len(answerable),
        "false_refusals": len(false_refusals),
        "false_refusal_rate": round(len(false_refusals) / len(answerable), 4)
        if answerable
        else None,
        "false_refusal_ids": [r.query["id"] for r in false_refusals],
        "accepted_unanswerable_ids": [
            r.query["id"] for r in unanswerable if r.retrieval.evidence.sufficient
        ],
        "term_coverage_answerable": summary(
            [r.retrieval.evidence.term_coverage for r in answerable]
        ),
        "term_coverage_unanswerable": summary(
            [r.retrieval.evidence.term_coverage for r in unanswerable]
        ),
    }


def _percentile(values: Sequence[float], share: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return round(ordered[index], 2)


def _load_dataset(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATASETS / name).read_text(encoding="utf-8"))
    return data


def _sources() -> dict[tuple[str, str], str]:
    sources = {}
    for path in sorted(KNOWLEDGE_BASE.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        metadata, _ = split_front_matter(text)
        sources[metadata["document_key"], metadata.get("version", "")] = text
    return sources


async def run_retrieval_suite(output: Path, *, database_url: str, quick: bool = False) -> Report:
    started = time.perf_counter()
    defaults = RetrievalOptions(top_k=DEPTH)
    production = replace(
        defaults,
        candidates=Settings.model_fields["rag_candidates"].default,
        rrf_k=Settings.model_fields["rag_rrf_k"].default,
        min_term_coverage=Settings.model_fields["rag_min_term_coverage"].default,
        min_dense_similarity=Settings.model_fields["rag_min_dense_similarity"].default,
    )
    configs = [
        Config("hybrid (production: ts_rank_cd order)", "sections", production),
        Config("hybrid, IDF order", "sections", replace(production, text_ranking="idf")),
        Config(
            "full text only (production: IDF order)",
            "sections",
            replace(production, use_dense=False),
        ),
        Config(
            "full text only, ts_rank_cd order",
            "sections",
            replace(production, use_dense=False, text_ranking="ts_rank"),
        ),
        Config("dense only (hashing)", "sections", replace(production, use_full_text=False)),
        Config("hybrid, no title/breadcrumb prefix", "no_prefix", production),
        Config("hybrid, fixed-size chunks", "fixed", production),
    ]
    if quick:
        configs = configs[:1]
    datasets = {
        "kb-queries": _load_dataset("kb-queries.json"),
        "kb-queries-holdout": _load_dataset("kb-queries-holdout.json"),
    }
    sources = _sources()
    markers = section_markers(sources)
    embedder = ChunkEmbedder(HashingEmbeddingProvider(), max_sensitivity=Sensitivity.INTERNAL)

    with tempfile.TemporaryDirectory(prefix="docintel-retrieval-eval-") as tmp:
        async with scratch_database(database_url) as url:
            engine = create_async_engine(url)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            try:
                settings = _settings(url, Path(tmp) / "storage")
                async with maker() as session:
                    users = await _users(session)
                await _ingest(maker, settings, users["admin"], LocalStorage(Path(tmp) / "storage"))
                corpora = {config.corpus for config in configs}
                for corpus in corpora - {"sections"}:
                    await _clone_corpus(maker, corpus, sources)
                async with maker() as session:
                    all_keys = list(await session.scalars(select(KnowledgeDocument.document_key)))
                    chunk_count = len((await session.scalars(select(KnowledgeChunk.id))).all())
                keys_by_corpus = {
                    "sections": sorted({k for k in all_keys if "~" not in k}),
                    "no_prefix": sorted({k for k in all_keys if k.endswith("~no_prefix")}),
                    "fixed": sorted({k for k in all_keys if k.endswith("~fixed")}),
                }
                corpus_stats: dict[str, dict[str, float]] = {}
                async with maker() as session:
                    for corpus, keys in keys_by_corpus.items():
                        if not keys:
                            continue
                        row = (
                            await session.execute(
                                select(
                                    func.count(KnowledgeChunk.id),
                                    func.avg(func.length(KnowledgeChunk.content)),
                                    func.max(func.length(KnowledgeChunk.content)),
                                )
                                .join(KnowledgeDocument)
                                .where(
                                    KnowledgeDocument.document_key.in_(keys),
                                    KnowledgeChunk.status == KnowledgeStatus.ACTIVE,
                                )
                            )
                        ).one()
                        corpus_stats[corpus] = {
                            "chunks": int(row[0]),
                            "mean_tokens": round(float(row[1] or 0) / 4, 1),
                            "max_tokens": round(float(row[2] or 0) / 4, 1),
                        }

                runs: dict[tuple[str, str], list[QueryResult]] = {}
                ideals: dict[tuple[str, str], dict[str, int]] = {}
                for config in configs:
                    keys = keys_by_corpus[config.corpus]
                    for name, dataset in datasets.items():
                        runs[config.name, name] = await _run(
                            maker,
                            embedder,
                            users["admin"],
                            dataset["queries"],
                            config,
                            keys,
                            markers,
                        )
                        ideals[config.name, name] = await _relevant_counts(
                            maker, dataset["queries"], config.corpus, keys, markers
                        )

                # Access control and versions, production configuration.
                every_query = [q for d in datasets.values() for q in d["queries"]]
                section_keys = keys_by_corpus["sections"]
                finance = await _run(
                    maker,
                    embedder,
                    users["finance"],
                    every_query,
                    configs[0],
                    section_keys,
                    markers,
                )
                async with maker() as session:
                    restricted = set(
                        await session.scalars(
                            select(KnowledgeDocument.id).where(
                                KnowledgeDocument.department_id.is_not(None),
                                KnowledgeDocument.department_id != users["finance"].department_id,
                            )
                        )
                    )
                leaks = sum(
                    1
                    for result in finance
                    for passage in result.retrieval.passages
                    if passage.knowledge_document_id in restricted
                )
                production_runs = [r for name in datasets for r in runs[configs[0].name, name]]
                superseded_now = sum(
                    1
                    for result in production_runs
                    for passage in result.retrieval.passages
                    if passage.status == KnowledgeStatus.SUPERSEDED
                )
                past = await _run(
                    maker,
                    embedder,
                    users["admin"],
                    every_query,
                    configs[0],
                    section_keys,
                    markers,
                    as_of=PAST_DATE,
                )
                future_in_past = sum(
                    1
                    for result in past
                    for passage in result.retrieval.passages
                    if passage.effective_from is not None and passage.effective_from > PAST_DATE
                )
                old_policy_cited = sum(
                    1
                    for result in past
                    for passage in result.retrieval.passages
                    if passage.status == KnowledgeStatus.SUPERSEDED
                )
            finally:
                await engine.dispose()

    metrics: dict[str, Any] = {"configs": {}, "gate": {}, "latency_ms": {}}
    ranking_rows: list[list[str]] = []
    for config in configs:
        metrics["configs"][config.name] = {}
        for name in datasets:
            values = ranking_metrics(runs[config.name, name], ideals[config.name, name])
            metrics["configs"][config.name][name] = values
            ranking_rows.append(
                [
                    config.name,
                    name,
                    str(values["questions"]),
                    pct(values["hit@1"]),
                    pct(values["hit@3"]),
                    pct(values["hit@5"]),
                    pct(values["section_recall@5"]),
                    pct(values["precision@5"]),
                    num(values["mrr"]),
                    num(values["ndcg@5"]),
                ]
            )
    gate_rows = []
    for name in datasets:
        gate = gate_metrics(runs[configs[0].name, name])
        metrics["gate"][name] = gate
        gate_rows.append(
            [
                name,
                f"{gate['correctly_refused']}/{gate['unanswerable']}",
                pct(gate["refusal_rate"]),
                f"{gate['false_refusals']}/{gate['answerable']}",
                pct(gate["false_refusal_rate"]),
                ", ".join(gate["false_refusal_ids"]) or "-",
                ", ".join(gate["accepted_unanswerable_ids"]) or "-",
            ]
        )
    totals = [r.retrieval.timings_ms["total"] for r in production_runs]
    metrics["latency_ms"] = {
        "p50": _percentile(totals, 0.5),
        "p95": _percentile(totals, 0.95),
        "max": round(max(totals), 2) if totals else 0.0,
        "questions": len(totals),
    }
    metrics["access_control"] = {
        "questions_as_finance_user": len(finance),
        "passages_from_other_departments": leaks,
    }
    metrics["versions"] = {
        "superseded_passages_on_eval_date": superseded_now,
        "passages_not_yet_in_force_on_past_date": future_in_past,
        "superseded_passages_on_past_date": old_policy_cited,
    }
    misses = [
        [
            r.query["id"],
            r.query["question"],
            ", ".join(label["section"] for label in r.query["relevant"]),
        ]
        for name in datasets
        for r in runs[configs[0].name, name]
        if r.query["relevant"] and not any(r.relevant[:5])
    ]
    tables: list[tuple[str, list[str], list[list[str]]]] = [
        (
            "Ranking (answerable questions; first 10 passages)",
            [
                "configuration",
                "dataset",
                "n",
                "hit@1",
                "hit@3",
                "hit@5",
                "section recall@5",
                "precision@5",
                "MRR",
                "nDCG@5",
            ],
            ranking_rows,
        ),
        (
            "Corpora (active versions; tokens estimated as characters / 4)",
            ["corpus", "chunks", "mean tokens per chunk", "max tokens"],
            [
                [
                    name,
                    str(int(stats["chunks"])),
                    str(stats["mean_tokens"]),
                    str(stats["max_tokens"]),
                ]
                for name, stats in corpus_stats.items()
            ],
        ),
        (
            "Evidence gate (production configuration)",
            [
                "dataset",
                "unanswerable refused",
                "refusal rate",
                "answerable refused",
                "false refusal rate",
                "falsely refused",
                "unanswerable accepted",
            ],
            gate_rows,
        ),
        (
            "Access control and versions (production configuration)",
            ["check", "result"],
            [
                [
                    f"passages of another department's documents returned to a Finance user "
                    f"({len(finance)} questions)",
                    str(leaks),
                ],
                [f"SUPERSEDED passages returned as of {EVAL_DATE}", str(superseded_now)],
                [f"passages not yet in force returned as of {PAST_DATE}", str(future_in_past)],
                [
                    f"passages returned as of {PAST_DATE} from the version then in force "
                    "(now SUPERSEDED)",
                    str(old_policy_cited),
                ],
            ],
        ),
        (
            "Latency (production configuration, local database, hashing embeddings)",
            ["p50 ms", "p95 ms", "max ms", "questions"],
            [[str(metrics["latency_ms"][key]) for key in ("p50", "p95", "max", "questions")]],
        ),
        (
            "Answerable questions with no relevant passage in the first 5 (production)",
            ["id", "question", "expected sections"],
            misses or [["-", "none", "-"]],
        ),
    ]
    notes = [
        "Embeddings: offline lexical hashing model (hashing-ngram-v1). It matches words and "
        "word fragments, not meaning, so 'dense' here is a second lexical signal. Gemini and "
        "fastembed (BAAI/bge-base-en-v1.5) embeddings: Not yet measured.",
        "kb-queries was used to choose the evidence-gate thresholds "
        f"(RAG_MIN_TERM_COVERAGE={production.min_term_coverage}, "
        f"RAG_MIN_DENSE_SIMILARITY={production.min_dense_similarity}) and the full-text "
        "ordering, so its numbers are optimistic. kb-queries-holdout was written afterwards "
        "and never used for tuning; it is small (10 answerable, 4 unanswerable questions).",
        "Relevance: a passage counts when it belongs to a labelled section (or a subsection). "
        "Fixed-size chunks have no sections; they count when they contain the section heading "
        "or the start of its first paragraph. Fixed-size chunks are larger on average and span "
        "several sections, which makes this criterion easier to meet; they also put more "
        "unrelated text into the model's context (see the corpora table).",
        "precision@5 is low by construction: most questions have one or two relevant sections, "
        "so at most one or two of five passages can be relevant.",
        f"Retrieval date fixed at {EVAL_DATE} (versions in force); version check also run as "
        f"of {PAST_DATE}. Generated answers (citation precision/recall, faithfulness) need an "
        "LLM: Not yet measured.",
    ]
    report = Report(
        quick=quick,
        suite="retrieval",
        title="Knowledge retrieval evaluation (RAG)",
        dataset={
            "knowledge_base": sorted(f"{key} {version}" for key, version in sources),
            "documents": len(sources),
            "chunks_all_corpora": chunk_count,
            "corpora": corpus_stats,
            "queries": {name: len(d["queries"]) for name, d in datasets.items()},
            "dataset_versions": {name: d.get("version") for name, d in datasets.items()},
            "eval_date": EVAL_DATE.isoformat(),
        },
        config={
            "embedding_model": HASHING_MODEL,
            "candidates": production.candidates,
            "rrf_k": production.rrf_k,
            "depth": DEPTH,
            "chunking": {
                "target_tokens": Settings.model_fields["knowledge_chunk_target_tokens"].default,
                "max_tokens": Settings.model_fields["knowledge_chunk_max_tokens"].default,
                "overlap_tokens": Settings.model_fields["knowledge_chunk_overlap_tokens"].default,
                "fixed_size_baseline_chars": FIXED_CHARS,
                "fixed_size_overlap_chars": FIXED_OVERLAP,
            },
            "quick": quick,
            "seconds": round(time.perf_counter() - started, 1),
        },
        metrics=metrics,
        environment=environment({"embedding": HASHING_MODEL}),
        notes=notes,
        tables=tables,
    )
    report.write(output)
    return report

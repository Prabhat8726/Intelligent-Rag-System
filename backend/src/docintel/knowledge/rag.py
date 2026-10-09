"""Knowledge questions end to end (Module 13): engines built once per API process, and the
service that retrieves, answers with citations and records the audit trail."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.ai.accounting import AccountedLLMProvider, DatabaseLLMCallLog
from docintel.ai.base import LLMProvider
from docintel.ai.registry import build_llm_provider, model_prices
from docintel.ai.routing import ExternalAIGate
from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.core.config import Settings
from docintel.db.models import AuditOutcome, Sensitivity, User
from docintel.knowledge.answering import Answer, AnswerGenerator
from docintel.knowledge.embedding import ChunkEmbedder, build_chunk_embedder
from docintel.knowledge.retrieval import (
    KnowledgeRetriever,
    KnowledgeScope,
    Retrieval,
    RetrievalOptions,
)


@dataclass(slots=True)
class RagEngines:
    embedder: ChunkEmbedder
    generator: AnswerGenerator
    options: RetrievalOptions

    async def aclose(self) -> None:
        await self.embedder.aclose()
        await self.generator.aclose()


def retrieval_options(settings: Settings) -> RetrievalOptions:
    return RetrievalOptions(
        candidates=settings.rag_candidates,
        top_k=settings.rag_top_k,
        rrf_k=settings.rag_rrf_k,
        min_term_coverage=settings.rag_min_term_coverage,
        min_dense_similarity=settings.rag_min_dense_similarity,
    )


def build_rag_engines(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    *,
    llm: LLMProvider | None = None,
    embedder: ChunkEmbedder | None = None,
) -> RagEngines:
    """Query embedder and answer model. With a sessionmaker, model calls are accounted in
    `llm_calls` and count against LLM_DAILY_REQUEST_BUDGET."""
    if llm is None and settings.rag_generation_enabled and settings.llm_configured:
        llm = build_llm_provider(settings)
    if llm is not None and sessionmaker is not None:
        log = DatabaseLLMCallLog(
            sessionmaker, daily_request_budget=settings.llm_daily_request_budget
        )
        llm = AccountedLLMProvider(llm, log, model_prices(settings))
    gate = ExternalAIGate(
        max_sensitivity=Sensitivity(settings.ai_external_max_sensitivity),
        provider_configured=llm is not None,
        provider_local=llm.local if llm is not None else False,
    )
    return RagEngines(
        embedder=embedder or build_chunk_embedder(settings),
        generator=AnswerGenerator(
            llm,
            gate,
            max_context_tokens=settings.rag_max_context_tokens,
            max_output_tokens=settings.rag_max_output_tokens,
        ),
        options=retrieval_options(settings),
    )


def today() -> date:
    return datetime.now(UTC).date()


class KnowledgeQueryService:
    def __init__(self, session: AsyncSession, engines: RagEngines) -> None:
        self._session = session
        self._engines = engines

    def _retriever(self, top_k: int | None = None) -> KnowledgeRetriever:
        options = self._engines.options
        if top_k is not None:
            options = RetrievalOptions(
                candidates=max(options.candidates, top_k),
                top_k=top_k,
                rrf_k=options.rrf_k,
                min_term_coverage=options.min_term_coverage,
                min_dense_similarity=options.min_dense_similarity,
            )
        return KnowledgeRetriever(self._session, self._engines.embedder, options)

    async def search(
        self, user: User, query: str, scope: KnowledgeScope, *, top_k: int | None = None
    ) -> Retrieval:
        """Passages only (no model call)."""
        return await self._retriever(top_k).retrieve(user, query, scope)

    async def ask(
        self, user: User, question: str, scope: KnowledgeScope, meta: RequestMeta
    ) -> tuple[Retrieval, Answer]:
        retrieval = await self._retriever().retrieve(user, question, scope)
        answer = await self._engines.generator.answer(question, retrieval)
        record_audit_event(
            self._session,
            action=AuditAction.KNOWLEDGE_QUERIED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=user,
            entity_type="knowledge",
            details={
                # The question may itself be sensitive: only its fingerprint is kept.
                "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
                "question_chars": len(question),
                "as_of": scope.as_of.isoformat(),
                "status": answer.status.value,
                "retrieval_mode": retrieval.mode,
                "evidence": {
                    "sufficient": retrieval.evidence.sufficient,
                    "term_coverage": round(retrieval.evidence.term_coverage, 4),
                    "dense_similarity": retrieval.evidence.dense_similarity,
                },
                "sources": [
                    {
                        "label": source.label,
                        "chunks": [str(p.chunk_id) for p in source.passages],
                        "knowledge_document_id": str(source.lead.knowledge_document_id),
                        "cited": source.label in answer.cited,
                        "sent_to_model": answer.model is not None and source not in answer.withheld,
                    }
                    for source in answer.sources
                ],
                "model": answer.model,
                "provider": answer.provider,
            },
        )
        await self._session.commit()
        return retrieval, answer

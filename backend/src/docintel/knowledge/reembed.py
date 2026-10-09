"""Re-embedding (`docintel reembed`): vectors for chunks that have none or another model's.

Needed after switching EMBEDDING_PROVIDER / model (queries only match chunks of the current
model), after a provider outage left documents full-text only, or after raising
AI_EXTERNAL_MAX_SENSITIVITY. One document per transaction, so an interrupted run resumes where
it stopped. The sensitivity gate applies exactly as at ingestion; chunks it blocks lose any
vector of a previous model (it could not match queries any more) and stay full-text only.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.ai.routing import max_sensitivity
from docintel.core.logging import get_logger
from docintel.db.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    KnowledgeChunk,
    KnowledgeDocument,
    Sensitivity,
)
from docintel.knowledge.embedding import ChunkEmbedder

logger = get_logger(__name__)


@dataclass(slots=True)
class ReembedSummary:
    knowledge_documents: int = 0
    business_documents: int = 0
    chunks_embedded: int = 0
    chunks_blocked: int = 0  # the sensitivity gate kept them full-text only


def _stale(model_column: Any, model: str, force: bool) -> Any:
    return true() if force else or_(model_column.is_(None), model_column != model)


async def _apply(
    session: AsyncSession,
    table: type[KnowledgeChunk] | type[DocumentChunk],
    ids: Sequence[uuid.UUID],
    vectors: list[list[float]] | None,
    model: str | None,
) -> None:
    for index, chunk_id in enumerate(ids):
        await session.execute(
            update(table)
            .where(table.id == chunk_id)
            .values(
                embedding=vectors[index] if vectors is not None else None,
                embedding_model=model if vectors is not None else None,
            )
        )


async def reembed(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: ChunkEmbedder,
    *,
    force: bool = False,
) -> ReembedSummary:
    model = embedder.model
    if model is None:
        msg = "No embedding provider is configured (EMBEDDING_PROVIDER / GEMINI_API_KEY)."
        raise ValueError(msg)
    summary = ReembedSummary()

    async with sessionmaker() as session:
        knowledge_ids = list(
            await session.scalars(
                select(KnowledgeChunk.knowledge_document_id)
                .where(_stale(KnowledgeChunk.embedding_model, model, force))
                .distinct()
            )
        )
    for document_id in knowledge_ids:
        async with sessionmaker() as session, session.begin():
            rows = (
                await session.execute(
                    select(
                        KnowledgeChunk.id,
                        KnowledgeChunk.context_prefix,
                        KnowledgeChunk.content,
                        KnowledgeChunk.sensitivity,
                    )
                    .where(KnowledgeChunk.knowledge_document_id == document_id)
                    .order_by(KnowledgeChunk.chunk_index)
                )
            ).all()
            if not rows:
                continue
            sensitivity = max_sensitivity(*(row.sensitivity for row in rows))
            texts = [f"{row.context_prefix}\n\n{row.content}" for row in rows]
            result = await embedder.embed_documents(texts, sensitivity)
            await _apply(session, KnowledgeChunk, [row.id for row in rows], result.vectors, model)
            await session.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.id == document_id)
                .values(
                    embedding_model=model if result.vectors is not None else None,
                    embedding_note=result.note,
                )
            )
            summary.knowledge_documents += 1
            if result.vectors is None:
                summary.chunks_blocked += len(rows)
            else:
                summary.chunks_embedded += len(rows)

    async with sessionmaker() as session:
        version_ids = list(
            await session.scalars(
                select(DocumentChunk.document_version_id)
                .where(_stale(DocumentChunk.embedding_model, model, force))
                .distinct()
            )
        )
    for version_id in version_ids:
        async with sessionmaker() as session, session.begin():
            labels = (
                await session.execute(
                    select(Document.sensitivity, DocumentVersion.sensitivity_assessment)
                    .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                    .where(DocumentVersion.id == version_id)
                )
            ).first()
            if labels is None:
                continue
            detected = (labels.sensitivity_assessment or {}).get("detected")
            sensitivity = max_sensitivity(
                labels.sensitivity, Sensitivity(detected) if detected else None
            )
            chunks = (
                await session.execute(
                    select(DocumentChunk.id, DocumentChunk.context_prefix, DocumentChunk.content)
                    .where(DocumentChunk.document_version_id == version_id)
                    .order_by(DocumentChunk.chunk_index)
                )
            ).all()
            texts = [f"{chunk.context_prefix}\n\n{chunk.content}" for chunk in chunks]
            result = await embedder.embed_documents(texts, sensitivity)
            ids = [chunk.id for chunk in chunks]
            await _apply(session, DocumentChunk, ids, result.vectors, model)
            summary.business_documents += 1
            if result.vectors is None:
                summary.chunks_blocked += len(chunks)
            else:
                summary.chunks_embedded += len(chunks)
    logger.info(
        "reembed.finished",
        model=model,
        knowledge_documents=summary.knowledge_documents,
        business_documents=summary.business_documents,
        chunks_embedded=summary.chunks_embedded,
        chunks_blocked=summary.chunks_blocked,
    )
    return summary

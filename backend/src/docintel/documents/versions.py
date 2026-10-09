"""Version history and clause-level comparison of two versions of a document (Module 27)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.core.errors import ConflictError, NotFoundError, UnprocessableContentError
from docintel.db.models import Document, DocumentPage, DocumentVersion
from docintel.versions.clauses import compare_clauses, segment, summarize


@dataclass(slots=True)
class VersionInfo:
    version: DocumentVersion
    processed: bool
    is_current: bool


class VersionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def versions(self, document: Document) -> list[VersionInfo]:
        processed = dict(
            (
                await self._session.execute(
                    select(DocumentPage.document_version_id, func.count())
                    .join(DocumentVersion, DocumentVersion.id == DocumentPage.document_version_id)
                    .where(DocumentVersion.document_id == document.id)
                    .group_by(DocumentPage.document_version_id)
                )
            ).all()
        )
        rows = await self._session.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document.id)
            .order_by(DocumentVersion.version_number.desc())
        )
        return [
            VersionInfo(row, bool(processed.get(row.id)), row.id == document.current_version_id)
            for row in rows
        ]

    async def _page_texts(self, document: Document, number: int) -> tuple[uuid.UUID, list[str]]:
        version = await self._session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version_number == number,
            )
        )
        if version is None:
            msg = f"Version {number} does not exist."
            raise NotFoundError(msg)
        texts = list(
            await self._session.scalars(
                select(DocumentPage.text)
                .where(DocumentPage.document_version_id == version.id)
                .order_by(DocumentPage.page_number)
            )
        )
        if not texts:
            msg = f"Version {number} has not been processed yet."
            raise ConflictError(msg)
        return version.id, texts

    async def compare(self, document: Document, old: int, new: int) -> dict[str, Any]:
        """Added, removed, modified and unchanged clauses between two versions."""
        if old == new:
            msg = "Choose two different versions."
            raise UnprocessableContentError(msg)
        old_id, old_texts = await self._page_texts(document, old)
        new_id, new_texts = await self._page_texts(document, new)
        diffs = compare_clauses(segment(old_texts), segment(new_texts))
        return {
            "document_id": document.id,
            "from_version": old,
            "to_version": new,
            "from_version_id": old_id,
            "to_version_id": new_id,
            "summary": summarize(diffs),
            "clauses": [diff.to_json() for diff in diffs],
        }

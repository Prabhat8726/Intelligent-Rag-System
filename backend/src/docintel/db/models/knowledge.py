"""Knowledge base (Module 12) and search index (Module 28): documents, chunks, vectors, FTS."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docintel.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from docintel.db.models.documents import Sensitivity
from docintel.db.models.identity import Department, User
from docintel.db.models.types import str_enum

EMBEDDING_DIMENSIONS = 768  # ADR-003: every embedding provider emits 768-d unit vectors

# Title/breadcrumb weigh more than the body ('A' > 'B'); 'english' stems ("payments" ~ "pay").
_SEARCH_EXPRESSION = (
    "setweight(to_tsvector('english', coalesce(context_prefix, '')), 'A') || "
    "setweight(to_tsvector('english', content), 'B')"
)


class KnowledgeCategory(StrEnum):
    POLICY = "POLICY"
    PROCEDURE = "PROCEDURE"
    CONTRACT_GUIDELINE = "CONTRACT_GUIDELINE"
    FAQ = "FAQ"
    COMPLIANCE = "COMPLIANCE"
    PUBLIC_REFERENCE = "PUBLIC_REFERENCE"


class KnowledgeStatus(StrEnum):
    PROCESSING = "PROCESSING"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"  # a newer version of the same document_key is active
    ARCHIVED = "ARCHIVED"  # deleted by an administrator
    FAILED = "FAILED"


class KnowledgeFormat(StrEnum):
    MARKDOWN = "MARKDOWN"
    TEXT = "TEXT"
    PDF = "PDF"
    PNG = "PNG"
    JPEG = "JPEG"
    TIFF = "TIFF"


class KnowledgeDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="effective_range",
        ),
        CheckConstraint("chunk_count >= 0", name="chunk_count_non_negative"),
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        # One active version per document key.
        Index(
            "uq_knowledge_documents_active_key",
            "document_key",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_knowledge_documents_status", "status"),
    )

    document_key: Mapped[str] = mapped_column(String(100), index=True)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[KnowledgeCategory] = mapped_column(str_enum(KnowledgeCategory, "category", 30))
    version_label: Mapped[str | None] = mapped_column(String(50))
    # NULL = organization-wide; otherwise readable by that department (and admins) only.
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), index=True
    )
    sensitivity: Mapped[Sensitivity] = mapped_column(str_enum(Sensitivity, "sensitivity"))
    # The higher of the label and what the content implies (card numbers, ...); set when
    # processed. Chunks carry it: it decides what may reach an external model.
    effective_sensitivity: Mapped[Sensitivity | None] = mapped_column(
        str_enum(Sensitivity, "effective_sensitivity")
    )
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[KnowledgeStatus] = mapped_column(
        str_enum(KnowledgeStatus, "status"), default=KnowledgeStatus.PROCESSING
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="SET NULL"), index=True
    )
    source_format: Mapped[KnowledgeFormat] = mapped_column(str_enum(KnowledgeFormat, "format", 10))
    original_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    storage_backend: Mapped[str] = mapped_column(String(10))
    storage_key: Mapped[str] = mapped_column(String(512))
    page_count: Mapped[int | None]
    chunk_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # Embedding model of the chunks, or NULL when they have no vectors (full-text only).
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    embedding_note: Mapped[str | None] = mapped_column(String(300))
    processing_error: Mapped[str | None] = mapped_column(String(1000))
    processed_at: Mapped[datetime | None]
    uploaded_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    deleted_at: Mapped[datetime | None]

    uploaded_by: Mapped[User] = relationship(lazy="selectin")
    department: Mapped[Department | None] = relationship(lazy="selectin")


class _ChunkColumns:
    chunk_index: Mapped[int] = mapped_column(Integer)
    # Title and section breadcrumb: indexed and embedded with the content, shown in citations.
    context_prefix: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    page_start: Mapped[int | None]
    page_end: Mapped[int | None]
    token_count: Mapped[int]
    content_hash: Mapped[str] = mapped_column(String(64))
    search: Mapped[str] = mapped_column(TSVECTOR, Computed(_SEARCH_EXPRESSION, persisted=True))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class KnowledgeChunk(UUIDPrimaryKeyMixin, _ChunkColumns, Base):
    """A section-scoped passage of a knowledge document. Filter columns are copied from the
    document so access, status and dates are filtered in the same index scan as the vector."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_document_id", "chunk_index", name="uq_knowledge_chunks_position"
        ),
        CheckConstraint("token_count >= 0", name="token_count_non_negative"),
        Index(
            "ix_knowledge_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_knowledge_chunks_search", "search", postgresql_using="gin"),
        Index("ix_knowledge_chunks_scope", "status", "department_id"),
    )

    knowledge_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True
    )
    section_path: Mapped[str] = mapped_column(Text)
    heading: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(10))
    status: Mapped[KnowledgeStatus] = mapped_column(str_enum(KnowledgeStatus, "status"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT")
    )
    category: Mapped[KnowledgeCategory] = mapped_column(str_enum(KnowledgeCategory, "category", 30))
    sensitivity: Mapped[Sensitivity] = mapped_column(str_enum(Sensitivity, "sensitivity"))
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)


class DocumentChunk(UUIDPrimaryKeyMixin, _ChunkColumns, Base):
    """A passage of a business document's current version, for semantic search (Module 28)."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "chunk_index", name="uq_document_chunks_position"),
        CheckConstraint("token_count >= 0", name="token_count_non_negative"),
        Index(
            "ix_document_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_document_chunks_search", "search", postgresql_using="gin"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )

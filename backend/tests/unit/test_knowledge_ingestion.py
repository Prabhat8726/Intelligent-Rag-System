"""Knowledge uploads: validation, metadata, version windows, the embedding gate."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from docintel.ai.base import EMBEDDING_DIMENSIONS, EmbeddingResult, EmbeddingTask
from docintel.ai.errors import ProviderUnavailableError
from docintel.ai.local_embeddings import HashingEmbeddingProvider
from docintel.core.errors import (
    PayloadTooLargeError,
    UnprocessableContentError,
    UnsupportedMediaTypeError,
)
from docintel.db.models import (
    KnowledgeCategory,
    KnowledgeDocument,
    KnowledgeFormat,
    Sensitivity,
)
from docintel.knowledge.embedding import NOT_CONFIGURED, ChunkEmbedder
from docintel.knowledge.lifecycle import is_newer, retrieval_window
from docintel.knowledge.sources import UnitKind, parse_text
from docintel.knowledge.validation import (
    MetadataInput,
    front_matter,
    resolve_metadata,
    slugify,
    validate_text_file,
)
from docintel.storage import StorageKeyError, knowledge_object_key


def write(tmp_path: Path, data: bytes, name: str = "upload.bin") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------------------ files
def test_text_files_are_decoded_and_normalized(tmp_path: Path) -> None:
    data = "﻿---\ncategory: FAQ\n---\r\n# Accounts Payable FAQ\r\n\r\nQ1.".encode()
    validated = validate_text_file(
        write(tmp_path, data), filename="../faq.md", content_type="text/markdown", max_bytes=10_000
    )
    assert validated.format == KnowledgeFormat.MARKDOWN
    assert validated.display_filename == "faq.md"
    assert validated.storage_extension == "md"
    assert validated.text is not None
    assert "\r" not in validated.text
    assert validated.text.startswith("---")  # byte-order mark removed
    assert front_matter(validated) == ({"category": "FAQ"}, "Accounts Payable FAQ")
    plain = validate_text_file(
        write(tmp_path, b"Retention\n\n1. Scope\n\nAll records."),
        filename="retention.txt",
        content_type=None,
        max_bytes=10_000,
    )
    assert plain.format == KnowledgeFormat.TEXT
    assert plain.mime_type == "text/plain"


@pytest.mark.parametrize(
    ("data", "filename", "content_type", "error"),
    [
        (b"x" * 2000, "big.md", "text/markdown", PayloadTooLargeError),
        (b"%PDF-1.7 not text", "fake.md", "text/markdown", UnsupportedMediaTypeError),
        (b"# Title", "policy.md", "image/png", UnsupportedMediaTypeError),
        (b"# Title", "policy.rtf", "text/plain", UnsupportedMediaTypeError),
        (b"\xc3\x28 invalid", "policy.md", "text/markdown", UnprocessableContentError),
        (b"# Title\x00", "policy.md", "text/markdown", UnprocessableContentError),
        (b" \n\n\t", "policy.md", "text/markdown", UnprocessableContentError),
    ],
)
def test_invalid_text_files(
    tmp_path: Path, data: bytes, filename: str, content_type: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        validate_text_file(
            write(tmp_path, data), filename=filename, content_type=content_type, max_bytes=1000
        )


def test_knowledge_storage_keys_are_server_generated() -> None:
    document_id = uuid.uuid4()
    assert knowledge_object_key(document_id, "md") == f"knowledge/{document_id}/original.md"
    with pytest.raises(StorageKeyError):
        knowledge_object_key(document_id, "../md")


# ------------------------------------------------------------------------------ metadata
def test_form_fields_win_over_front_matter_and_defaults_apply() -> None:
    front = {
        "title": "Procurement Policy",
        "document_key": "procurement-policy",
        "category": "policy",
        "version": "2026.1",
        "effective_from": "2026-01-01",
        "department": "Legal",
    }
    metadata = resolve_metadata(
        MetadataInput(version_label="2026.2", sensitivity=Sensitivity.CONFIDENTIAL),
        front,
        first_heading="Ignored",
        filename="policy.md",
    )
    assert metadata.title == "Procurement Policy"
    assert not metadata.title_from_content
    assert metadata.document_key == "procurement-policy"
    assert metadata.category == KnowledgeCategory.POLICY  # case-insensitive
    assert metadata.version_label == "2026.2"
    assert metadata.sensitivity == Sensitivity.CONFIDENTIAL
    assert metadata.effective_from == date(2026, 1, 1)
    assert metadata.department_name == "Legal"

    minimal = resolve_metadata(
        MetadataInput(category=KnowledgeCategory.FAQ),
        {},
        first_heading=None,
        filename="Supplier_onboarding-FAQ.pdf",
    )
    assert minimal.title == "Supplier onboarding FAQ"
    assert minimal.title_from_content  # a PDF's first heading replaces it when processed
    assert minimal.document_key == "supplier-onboarding-faq"
    assert minimal.sensitivity == Sensitivity.INTERNAL


@pytest.mark.parametrize(
    ("form", "front", "message"),
    [
        (MetadataInput(), {"title": "T"}, "category is required"),
        (MetadataInput(), {"category": "MEMO"}, "category must be one of"),
        (MetadataInput(document_key="Bad Key!"), {"category": "FAQ"}, "document_key"),
        (MetadataInput(), {"category": "FAQ", "sensitivity": "SECRET"}, "sensitivity"),
        (MetadataInput(), {"category": "FAQ", "effective_from": "01/02/2026"}, "YYYY-MM-DD"),
        (
            MetadataInput(effective_from=date(2026, 2, 1), effective_to=date(2026, 1, 1)),
            {"category": "FAQ"},
            "effective_to",
        ),
        (MetadataInput(title="x" * 301), {"category": "FAQ"}, "title"),
    ],
)
def test_invalid_metadata(form: MetadataInput, front: dict[str, str], message: str) -> None:
    with pytest.raises(UnprocessableContentError, match=message):
        resolve_metadata(form, front, first_heading="Heading", filename="f.md")


def test_slugify() -> None:
    assert slugify("Travel & Expense Policy (2026)") == "travel-expense-policy-2026"
    assert len(slugify("word " * 50)) <= 100


def test_plain_text_headings() -> None:
    source = parse_text("Records Policy\n\n1. Scope\n\nAll records.\nEvery year.\n\n2.1 Invoices")
    assert [(u.kind, u.level, u.text) for u in source.units] == [
        (UnitKind.HEADING, 1, "Records Policy"),
        (UnitKind.HEADING, 2, "1. Scope"),
        (UnitKind.TEXT, 0, "All records.\nEvery year."),
        (UnitKind.HEADING, 3, "2.1 Invoices"),
    ]
    # A sentence on the first line is text, not a title.
    assert parse_text("Invoices are paid weekly.").units[0].kind == UnitKind.TEXT


# ------------------------------------------------------------------------------ versions
def version(
    effective_from: date | None,
    effective_to: date | None = None,
    *,
    replaced: bool = False,
    published: date = date(2026, 3, 15),
) -> KnowledgeDocument:
    return KnowledgeDocument(
        effective_from=effective_from,
        effective_to=effective_to,
        supersedes_id=uuid.uuid4() if replaced else None,
        processed_at=datetime(published.year, published.month, published.day, 9, tzinfo=UTC),
    )


def test_newer_versions() -> None:
    assert is_newer(date(2026, 1, 1), date(2025, 1, 1))
    assert is_newer(date(2026, 1, 1), date(2026, 1, 1))  # a same-day correction replaces
    assert not is_newer(date(2025, 1, 1), date(2026, 1, 1))  # a historical upload
    assert is_newer(None, date(2026, 1, 1))
    assert is_newer(date(2026, 1, 1), None)


def test_retrieval_windows() -> None:
    old = version(date(2025, 1, 1), date(2025, 12, 31))
    new = version(date(2026, 1, 1), replaced=True)
    assert retrieval_window(new, None) == (date(2026, 1, 1), None)
    assert retrieval_window(old, new) == (date(2025, 1, 1), date(2025, 12, 31))
    # A future-dated version caps the current one the day before it starts.
    current = version(date(2026, 1, 1))
    future = version(date(2026, 11, 1), replaced=True)
    assert retrieval_window(current, future) == (date(2026, 1, 1), date(2026, 10, 31))
    # Without dates, a replacement starts when it was published; the first version has no start.
    undated = version(None)
    replacement = version(None, replaced=True, published=date(2026, 4, 2))
    assert retrieval_window(replacement, None) == (date(2026, 4, 2), None)
    assert retrieval_window(undated, replacement) == (None, date(2026, 4, 1))
    # Replaced from its first day: an empty window (end before start) matches no date.
    start, end = retrieval_window(
        version(date(2026, 1, 1)), version(date(2026, 1, 1), replaced=True)
    )
    assert start is not None
    assert end is not None
    assert end < start


# ------------------------------------------------------------------------------ embedder
class External:
    name = "external"
    local = False
    model = "external-768"
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], EmbeddingTask]] = []
        self.fail = False

    async def embed(self, texts: Sequence[str], task: EmbeddingTask) -> EmbeddingResult:
        self.calls.append((list(texts), task))
        if self.fail:
            raise ProviderUnavailableError("down", provider=self.name)
        vectors = [[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _ in texts]
        return EmbeddingResult(vectors, self.model, self.dimensions, 1.0)

    async def aclose(self) -> None:
        return None


async def test_embedder_batches_and_gates_documents() -> None:
    provider = External()
    embedder = ChunkEmbedder(provider, max_sensitivity=Sensitivity.INTERNAL, batch_size=2)
    result = await embedder.embed_documents(["a", "b", "c"], Sensitivity.INTERNAL)
    assert result.vectors is not None
    assert len(result.vectors) == 3
    assert result.model == "external-768"
    assert [len(texts) for texts, _ in provider.calls] == [2, 1]
    blocked = await embedder.embed_documents(["secret"], Sensitivity.CONFIDENTIAL)
    assert blocked.vectors is None
    assert blocked.note is not None
    assert "CONFIDENTIAL" in blocked.note
    assert len(provider.calls) == 2  # nothing was sent
    local = ChunkEmbedder(HashingEmbeddingProvider(), max_sensitivity=Sensitivity.PUBLIC)
    restricted = await local.embed_documents(["Card policy"], Sensitivity.RESTRICTED)
    assert restricted.vectors is not None  # a local model keeps content in the deployment
    none = await ChunkEmbedder(None, max_sensitivity=Sensitivity.INTERNAL).embed_documents(
        ["text"], Sensitivity.PUBLIC
    )
    assert (none.vectors, none.note) == (None, NOT_CONFIGURED)


async def test_query_embedding_falls_back_to_full_text() -> None:
    provider = External()
    embedder = ChunkEmbedder(provider, max_sensitivity=Sensitivity.INTERNAL)
    assert await embedder.embed_query("What is the price tolerance?") is not None
    assert provider.calls[-1][1] == EmbeddingTask.RETRIEVAL_QUERY
    # A card number in the question is not sent to an external provider.
    assert await embedder.embed_query("Who paid with card 4111 1111 1111 1111?") is None
    assert len(provider.calls) == 1
    provider.fail = True
    assert await embedder.embed_query("What is the price tolerance?") is None
    assert await ChunkEmbedder(None, max_sensitivity=Sensitivity.INTERNAL).embed_query("q") is None

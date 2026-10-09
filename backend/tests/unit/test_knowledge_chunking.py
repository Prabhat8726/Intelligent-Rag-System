"""Knowledge sources and structure-aware chunking (Module 12)."""

from __future__ import annotations

import itertools
from datetime import date
from pathlib import Path

import pytest

from docintel.knowledge.chunking import (
    PATH_SEPARATOR,
    ChunkingOptions,
    chunk_source,
    estimate_tokens,
)
from docintel.knowledge.sources import UnitKind, parse_markdown, parse_pages, split_front_matter
from docintel.processing.content import BBox, PageContent, Word
from docintel.processing.inspection import PageMethod
from docintel.processing.layout import analyze_layout

KNOWLEDGE_BASE = Path(__file__).resolve().parents[3] / "knowledge_base"
CATEGORIES = {"POLICY", "PROCEDURE", "CONTRACT_GUIDELINE", "FAQ", "COMPLIANCE", "PUBLIC_REFERENCE"}

POLICY = """---
title: Procurement Policy
document_key: procurement-policy
version: 2026.1
---

# Procurement Policy

Intro paragraph.

## 4. Price variance

### 4.1 Tolerance

Prices must match. A difference of 0.01 is accepted.

### 4.2 Price increases

Only with a change order.

## 5. Approval limits

| Value | Approver |
| --- | --- |
| up to 5,000 | Manager |
"""


def test_front_matter_is_split_off() -> None:
    metadata, body = split_front_matter(POLICY)
    assert metadata == {
        "title": "Procurement Policy",
        "document_key": "procurement-policy",
        "version": "2026.1",
    }
    assert body.lstrip().startswith("# Procurement Policy")
    assert split_front_matter("# No front matter") == ({}, "# No front matter")
    with pytest.raises(ValueError, match="not closed"):
        split_front_matter("---\ntitle: x\n")
    with pytest.raises(ValueError, match="key: value"):
        split_front_matter("---\njust text\n---\n")


def test_markdown_units() -> None:
    source = parse_markdown(POLICY)
    kinds = [(unit.kind, unit.level) for unit in source.units]
    assert kinds[:3] == [(UnitKind.HEADING, 1), (UnitKind.TEXT, 0), (UnitKind.HEADING, 2)]
    table = next(unit for unit in source.units if unit.kind == UnitKind.TABLE)
    assert table.text == "| Value | Approver |\n| up to 5,000 | Manager |"  # separator row dropped
    assert source.title == "Procurement Policy"


def test_chunks_follow_sections_with_breadcrumbs() -> None:
    chunks = chunk_source(parse_markdown(POLICY))
    paths = [chunk.section_path for chunk in chunks]
    assert paths == [
        (),
        ("4. Price variance", "4.1 Tolerance"),
        ("4. Price variance", "4.2 Price increases"),
        ("5. Approval limits",),
    ]
    tolerance = chunks[1]
    assert tolerance.content == "Prices must match. A difference of 0.01 is accepted."
    assert tolerance.heading == "4.1 Tolerance"
    assert tolerance.breadcrumb == f"4. Price variance{PATH_SEPARATOR}4.1 Tolerance"
    assert tolerance.context_text.startswith(f"Procurement Policy\n{tolerance.breadcrumb}\n\n")
    assert chunks[3].kind == "table"
    assert len({chunk.content_hash for chunk in chunks}) == len(chunks)


def test_long_sections_are_split_at_sentences_with_overlap() -> None:
    sentences = [
        f"Rule number {i} applies to every purchase order in the company." for i in range(40)
    ]
    text = "# Policy\n\n## 1. Rules\n\n" + " ".join(sentences) + "\n\n## 2. Next\n\nShort."
    options = ChunkingOptions(target_tokens=120, max_tokens=160, overlap_tokens=40)
    chunks = chunk_source(parse_markdown(text), options)
    rules = [chunk for chunk in chunks if chunk.section_path == ("1. Rules",)]
    assert len(rules) > 3
    assert all(estimate_tokens(chunk.content) <= options.max_tokens for chunk in rules)
    for first, second in itertools.pairwise(rules):
        last_sentence = first.content.split("\n\n")[-1]
        assert last_sentence in second.content.split("\n\n")[0]  # overlap
    assert chunks[-1].section_path == ("2. Next",)
    assert chunks[-1].content == "Short."  # no overlap across sections


def test_long_tables_repeat_their_header() -> None:
    rows = "\n".join(f"| item {i} | {i * 10} USD |" for i in range(80))
    text = f"# T\n\n## 1. Prices\n\n| Item | Price |\n| --- | --- |\n{rows}\n"
    chunks = chunk_source(parse_markdown(text), ChunkingOptions(100, 120, 20))
    assert len(chunks) > 2
    assert all(chunk.content.startswith("| Item | Price |") for chunk in chunks)
    assert all(chunk.kind == "table" for chunk in chunks)


def test_chunking_options_are_validated() -> None:
    with pytest.raises(ValueError, match="target_tokens"):
        ChunkingOptions(target_tokens=900, max_tokens=800)
    with pytest.raises(ValueError, match="overlap_tokens"):
        ChunkingOptions(target_tokens=100, max_tokens=200, overlap_tokens=100)


def _line(text: str, y: float, size: float = 10.0) -> list[Word]:
    words: list[Word] = []
    x = 50.0
    for token in text.split():
        words.append(Word(token, BBox(x, y, x + len(token) * 0.5 * size, y + size), None, size))
        x = words[-1].bbox.x1 + 0.3 * size
    return words


def test_pages_give_numbered_and_large_headings() -> None:
    words = [
        *_line("Retention Policy", 40, size=16),
        *_line("Records are kept as listed below.", 80),
        *_line("2. Retention periods", 120),
        *_line("Invoices are kept for ten years.", 135),
    ]
    page = analyze_layout(PageContent(3, 600, 800, "pt", PageMethod.NATIVE, words))
    source = parse_pages([page])
    assert [(u.kind, u.level, u.text) for u in source.units if u.kind == UnitKind.HEADING] == [
        (UnitKind.HEADING, 1, "Retention Policy"),
        (UnitKind.HEADING, 2, "2. Retention periods"),
    ]
    chunks = chunk_source(source)
    assert chunks[-1].section_path == ("2. Retention periods",)
    assert chunks[-1].content == "Invoices are kept for ten years."
    assert (chunks[-1].page_start, chunks[-1].page_end) == (3, 3)
    assert chunks[0].title == "Retention Policy"


@pytest.mark.parametrize("path", sorted(KNOWLEDGE_BASE.glob("*.md")), ids=lambda p: p.name)
def test_seed_knowledge_base_is_well_formed(path: Path) -> None:
    source = parse_markdown(path.read_text(encoding="utf-8"))
    metadata = source.metadata
    assert {"title", "document_key", "version", "category", "effective_from", "sensitivity"} <= set(
        metadata
    )
    assert metadata["category"] in CATEGORIES
    date.fromisoformat(metadata["effective_from"])
    chunks = chunk_source(source)
    assert chunks
    assert all(chunk.token_count <= ChunkingOptions().max_tokens + 50 for chunk in chunks)
    assert all("SYNTHETIC DOCUMENT" not in chunk.content for chunk in chunks[1:])

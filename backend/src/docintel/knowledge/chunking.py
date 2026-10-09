"""Structure-aware chunking (Module 12): sections first, then paragraphs, sentences, words.

* A chunk never crosses a section boundary, so every chunk cites one section and its breadcrumb
  ("4. Price variance > 4.1 Tolerance") is exact.
* Inside a section, paragraphs are packed up to `target_tokens`; a paragraph longer than
  `max_tokens` is split at sentences (then words). Consecutive chunks of one section overlap by
  about `overlap_tokens` of whole sentences, so a fact on a boundary is in both.
* Tables stay whole up to `max_tokens`; longer tables are split into row groups that repeat the
  header row.
* The text that is embedded and full-text indexed carries the document title and the section
  breadcrumb in front of the chunk (contextual chunking); the stored content does not.

Token counts are estimated as ceil(characters / 4), the usual average for English text with
current tokenizers; budgets are approximate by design and far below every model's input limit.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from docintel.knowledge.sources import ParsedSource, Unit, UnitKind

PATH_SEPARATOR = " \u203a "  # single right-pointing angle quotation mark
_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+(?=[A-Z0-9(\"'])")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


@dataclass(frozen=True, slots=True)
class ChunkingOptions:
    target_tokens: int = 500
    max_tokens: int = 800
    overlap_tokens: int = 75  # ~15% of the target

    def __post_init__(self) -> None:
        if not 0 < self.target_tokens <= self.max_tokens:
            msg = "need 0 < target_tokens <= max_tokens"
            raise ValueError(msg)
        if not 0 <= self.overlap_tokens < self.target_tokens:
            msg = "need 0 <= overlap_tokens < target_tokens"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    title: str
    section_path: tuple[str, ...]
    content: str
    page_start: int | None
    page_end: int | None
    kind: str = "text"  # "text" or "table"

    @property
    def heading(self) -> str:
        return self.section_path[-1] if self.section_path else self.title

    @property
    def breadcrumb(self) -> str:
        return PATH_SEPARATOR.join(self.section_path)

    @property
    def context_text(self) -> str:
        """Embedded and indexed text: title and breadcrumb in front of the content."""
        lines = [self.title]
        if self.section_path:
            lines.append(self.breadcrumb)
        return "\n".join(lines) + "\n\n" + self.content

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.context_text)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.context_text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class _Section:
    path: tuple[str, ...]
    units: list[Unit] = field(default_factory=list)


def _sections(units: Sequence[Unit]) -> list[_Section]:
    """Group units under the heading path they belong to (the level-1 title is the root)."""
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    current = _Section(())
    for unit in units:
        if unit.kind == UnitKind.HEADING:
            if unit.level <= 1:
                continue  # the document title: shown with every chunk, not part of the path
            if current.units:
                sections.append(current)
            while stack and stack[-1][0] >= unit.level:
                stack.pop()
            stack.append((unit.level, unit.text))
            current = _Section(tuple(title for _, title in stack))
        else:
            current.units.append(unit)
    if current.units:
        sections.append(current)
    return sections


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_END.split(text) if part.strip()]


def _split_long(text: str, max_tokens: int) -> list[str]:
    """Pieces of at most max_tokens: by sentence, then by words."""
    pieces: list[str] = []
    for sentence in _sentences(text) or [text]:
        if estimate_tokens(sentence) <= max_tokens:
            pieces.append(sentence)
            continue
        words: list[str] = []
        for word in sentence.split():
            if words and estimate_tokens(" ".join([*words, word])) > max_tokens:
                pieces.append(" ".join(words))
                words = []
            words.append(word)
        if words:
            pieces.append(" ".join(words))
    return pieces


def _table_pieces(text: str, max_tokens: int) -> list[str]:
    if estimate_tokens(text) <= max_tokens:
        return [text]
    header, *rows = text.split("\n")
    pieces: list[str] = []
    group: list[str] = []
    for row in rows:
        if group and estimate_tokens("\n".join([header, *group, row])) > max_tokens:
            pieces.append("\n".join([header, *group]))
            group = []
        group.append(row)
    if group:
        pieces.append("\n".join([header, *group]))
    return pieces


@dataclass(slots=True)
class _Piece:
    text: str
    page: int | None
    table: bool = False


def _pieces(section: _Section, options: ChunkingOptions) -> list[_Piece]:
    pieces: list[_Piece] = []
    for unit in section.units:
        if unit.kind == UnitKind.TABLE:
            pieces += [
                _Piece(p, unit.page, True) for p in _table_pieces(unit.text, options.max_tokens)
            ]
        elif estimate_tokens(unit.text) > options.max_tokens:
            pieces += [_Piece(p, unit.page) for p in _split_long(unit.text, options.max_tokens)]
        else:
            pieces.append(_Piece(unit.text, unit.page))
    return pieces


def _overlap(previous: list[_Piece], budget: int) -> list[_Piece]:
    """The last whole sentences of a chunk, up to `budget` tokens (never a table, never the
    whole chunk)."""
    if budget == 0 or not previous or previous[-1].table:
        return []
    trailing: list[_Piece] = []
    for piece in reversed(previous):  # the text after the last table
        if piece.table:
            break
        trailing.insert(0, piece)
    sentences = [sentence for piece in trailing for sentence in _sentences(piece.text)]
    tail: list[str] = []
    for sentence in reversed(sentences[1:]):
        if estimate_tokens(" ".join([sentence, *tail])) > budget:
            break
        tail.insert(0, sentence)
    return [_Piece(" ".join(tail), previous[-1].page)] if tail else []


def chunk_source(
    source: ParsedSource, options: ChunkingOptions | None = None, *, title: str | None = None
) -> list[Chunk]:
    options = options or ChunkingOptions()
    document_title = title or source.title or "Untitled"
    chunks: list[Chunk] = []

    def emit(path: tuple[str, ...], pieces: list[_Piece]) -> None:
        pages = [piece.page for piece in pieces if piece.page is not None]
        chunks.append(
            Chunk(
                index=len(chunks),
                title=document_title,
                section_path=path,
                content="\n\n".join(piece.text for piece in pieces),
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
                kind="table" if all(piece.table for piece in pieces) else "text",
            )
        )

    for section in _sections(source.units):
        current: list[_Piece] = []
        fresh = 0  # pieces in `current` that are not overlap from the previous chunk
        for piece in _pieces(section, options):
            candidate = "\n\n".join(p.text for p in [*current, piece])
            if fresh and estimate_tokens(candidate) > options.target_tokens:
                emit(section.path, current)
                current = _overlap(current, options.overlap_tokens)
                fresh = 0
            current.append(piece)
            fresh += 1
        if fresh:
            emit(section.path, current)
    return chunks

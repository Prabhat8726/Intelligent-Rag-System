"""Normalized page content (Module 3): the single representation every later stage consumes.

Native PDF text and OCR output are converted into the same shapes, so layout analysis, table
detection, classification and (Phase 4) extraction never care where the text came from.

Coordinates: top-left origin, in page units - PDF points ("pt") for PDF pages, pixels ("px")
for image uploads - in the page's displayed orientation (after any rotation correction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from docintel.processing.inspection import PageMethod

CONTENT_VERSION = 1
_ROUND = 2


class BlockKind(StrEnum):
    HEADING = "heading"
    TEXT = "text"
    TABLE = "table"


@dataclass(frozen=True, slots=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2

    def union(self, other: BBox) -> BBox:
        return BBox(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def to_list(self) -> list[float]:
        return [
            round(self.x0, _ROUND),
            round(self.y0, _ROUND),
            round(self.x1, _ROUND),
            round(self.y1, _ROUND),
        ]

    @classmethod
    def from_list(cls, values: list[float]) -> BBox:
        return cls(*values)

    @classmethod
    def enclosing(cls, boxes: list[BBox]) -> BBox:
        if not boxes:
            msg = "cannot enclose zero boxes"
            raise ValueError(msg)
        return BBox(
            min(box.x0 for box in boxes),
            min(box.y0 for box in boxes),
            max(box.x1 for box in boxes),
            max(box.y1 for box in boxes),
        )


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    bbox: BBox
    # OCR confidence 0-100; None for text taken from the PDF text layer.
    confidence: float | None
    # Font size (native text) or an estimate from the glyph height (OCR), in page units.
    size: float

    def to_list(self) -> list[Any]:
        confidence = None if self.confidence is None else round(self.confidence, 1)
        return [self.text, *self.bbox.to_list(), confidence, round(self.size, _ROUND)]

    @classmethod
    def from_list(cls, values: list[Any]) -> Word:
        text, x0, y0, x1, y1, confidence, size = values
        return cls(text, BBox(x0, y0, x1, y1), confidence, size)


@dataclass(frozen=True, slots=True)
class Line:
    """Words sharing a baseline, split into segments wherever the gap is column-sized."""

    segments: list[list[int]]  # word indices (into PageContent.words), left to right
    bbox: BBox

    @property
    def word_indices(self) -> list[int]:
        return [index for segment in self.segments for index in segment]

    def to_json(self) -> dict[str, Any]:
        return {"segments": self.segments, "bbox": self.bbox.to_list()}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Line:
        return cls(segments=data["segments"], bbox=BBox.from_list(data["bbox"]))


@dataclass(frozen=True, slots=True)
class Block:
    """A reading-order unit: a heading, a paragraph-like text block or a table."""

    kind: BlockKind
    bbox: BBox
    # (line index, segment index) pairs for text blocks; empty for tables.
    segments: list[tuple[int, int]] = field(default_factory=list)
    table: int | None = None  # index into PageContent.tables

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "bbox": self.bbox.to_list(),
            "segments": [list(pair) for pair in self.segments],
            "table": self.table,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Block:
        return cls(
            kind=BlockKind(data["kind"]),
            bbox=BBox.from_list(data["bbox"]),
            segments=[(pair[0], pair[1]) for pair in data["segments"]],
            table=data["table"],
        )


@dataclass(frozen=True, slots=True)
class TableRow:
    cells: list[str]
    bbox: BBox
    page_number: int

    def to_json(self) -> dict[str, Any]:
        return {"cells": self.cells, "bbox": self.bbox.to_list(), "page_number": self.page_number}


@dataclass(frozen=True, slots=True)
class PageTable:
    """A table detected on one page (multi-page tables are stitched at document level)."""

    page_number: int
    bbox: BBox
    header: list[str]
    rows: list[TableRow]
    column_bounds: list[float]  # x positions separating the columns
    confidence: float

    def to_json(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "bbox": self.bbox.to_list(),
            "header": self.header,
            "rows": [row.to_json() for row in self.rows],
            "column_bounds": [round(bound, _ROUND) for bound in self.column_bounds],
            "confidence": round(self.confidence, 4),
        }


@dataclass(slots=True)
class PageContent:
    page_number: int
    width: float
    height: float
    unit: str
    method: PageMethod
    words: list[Word]
    # Clockwise rotation (degrees) applied to the page image before OCR, 0 for native pages.
    rotation_applied: int = 0
    ocr_confidence: float | None = None
    lines: list[Line] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    tables: list[PageTable] = field(default_factory=list)
    text: str = ""
    warnings: list[str] = field(default_factory=list)
    preview_key: str | None = None
    preview_size: tuple[int, int] | None = None
    # Local preview image written during extraction (uploaded to storage, never persisted).
    preview_file: Path | None = None

    def segment_text(self, line_index: int, segment_index: int) -> str:
        segment = self.lines[line_index].segments[segment_index]
        return " ".join(self.words[index].text for index in segment)

    def layout_json(self) -> dict[str, Any]:
        return {
            "version": CONTENT_VERSION,
            "lines": [line.to_json() for line in self.lines],
            "blocks": [block.to_json() for block in self.blocks],
            "warnings": self.warnings,
        }


@dataclass(frozen=True, slots=True)
class DocumentTable:
    """A table after multi-page stitching."""

    page_start: int
    page_end: int
    bbox: BBox  # on page_start
    header: list[str]
    rows: list[TableRow]
    method: str  # NATIVE | OCR | MIXED
    confidence: float


def mean_confidence(words: list[Word]) -> float | None:
    """Character-weighted mean OCR confidence (None when no OCR words)."""
    weighted = [(word.confidence, len(word.text)) for word in words if word.confidence is not None]
    total = sum(length for _, length in weighted)
    if total == 0:
        return None
    return sum(confidence * length for confidence, length in weighted) / total

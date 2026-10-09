"""Knowledge sources to structured text units (headings, paragraphs, tables) with page numbers.

Markdown (and plain text) is parsed directly: `#` headings, `|` tables, blank-line paragraphs, an
optional front-matter block of `key: value` lines between `---` fences. PDFs and images go
through the same extraction as business documents; their units come from the layout blocks:
large single-line blocks and numbered headings ("4.", "4.2 Tolerance", "Article 3") become
headings, detected tables stay tables.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from docintel.processing.content import BlockKind, PageContent
from docintel.versions.clauses import parse_heading

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FRONT_MATTER_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


class UnitKind(StrEnum):
    HEADING = "heading"
    TEXT = "text"
    TABLE = "table"


@dataclass(frozen=True, slots=True)
class Unit:
    kind: UnitKind
    text: str
    level: int = 0  # headings: 1 = document title, 2 = top-level section, ...
    page: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedSource:
    metadata: dict[str, str]
    units: list[Unit]

    @property
    def title(self) -> str | None:
        heading = next((u for u in self.units if u.kind == UnitKind.HEADING), None)
        return self.metadata.get("title") or (heading.text if heading else None)


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """`key: value` lines between leading `---` fences (no nesting, no YAML types)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    metadata: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return metadata, "\n".join(lines[index + 1 :])
        match = _FRONT_MATTER_KEY.match(line.strip())
        if match:
            metadata[match.group(1).lower()] = match.group(2).strip().strip("\"'")
        elif line.strip():
            msg = f"front matter line {index + 1} is not 'key: value'"
            raise ValueError(msg)
    msg = "front matter is not closed with '---'"
    raise ValueError(msg)


def parse_markdown(text: str) -> ParsedSource:
    metadata, body = split_front_matter(text)
    units: list[Unit] = []
    paragraph: list[str] = []
    table: list[str] = []

    def flush() -> None:
        if paragraph:
            units.append(Unit(UnitKind.TEXT, "\n".join(paragraph)))
            paragraph.clear()
        if table:
            rows = [row for row in table if not _TABLE_SEPARATOR.match(row)]
            units.append(Unit(UnitKind.TABLE, "\n".join(rows)))
            table.clear()

    for raw in body.splitlines():
        line = raw.rstrip()
        heading = _MD_HEADING.match(line)
        if heading:
            flush()
            units.append(Unit(UnitKind.HEADING, heading.group(2), level=len(heading.group(1))))
        elif line.lstrip().startswith("|"):
            if paragraph:
                flush()
            table.append(line.strip())
        elif not line.strip():
            flush()
        else:
            if table:
                flush()
            paragraph.append(line.strip())
    flush()
    return ParsedSource(metadata, units)


def _heading_level(number: str | None) -> int:
    """'4' -> 2, '4.2' -> 3 (the document title is level 1)."""
    return 2 if number is None else number.count(".") + 2


def parse_pages(pages: Sequence[PageContent]) -> ParsedSource:
    """Units from extracted pages, in reading order, with their page numbers."""
    units: list[Unit] = []
    for page in pages:
        for block in page.blocks:
            if block.kind == BlockKind.TABLE and block.table is not None:
                table = page.tables[block.table]
                rows = [table.header, *(row.cells for row in table.rows)]
                text = "\n".join("| " + " | ".join(cells) + " |" for cells in rows)
                units.append(Unit(UnitKind.TABLE, text, page=page.page_number))
                continue
            lines: dict[int, list[str]] = {}
            for line_index, segment_index in block.segments:
                lines.setdefault(line_index, []).append(
                    page.segment_text(line_index, segment_index)
                )
            texts = ["  ".join(parts) for _, parts in sorted(lines.items())]
            if block.kind == BlockKind.HEADING and texts:
                level = 1 if not any(u.kind == UnitKind.HEADING for u in units) else 2
                units.append(Unit(UnitKind.HEADING, texts[0], level=level, page=page.page_number))
                continue
            paragraph: list[str] = []
            for text in texts:
                numbered = parse_heading(text)
                if numbered is not None:
                    if paragraph:
                        units.append(
                            Unit(UnitKind.TEXT, " ".join(paragraph), page=page.page_number)
                        )
                        paragraph = []
                    number, title = numbered
                    units.append(
                        Unit(
                            UnitKind.HEADING,
                            f"{number}. {title}" if "." not in number else f"{number} {title}",
                            level=_heading_level(number),
                            page=page.page_number,
                        )
                    )
                else:
                    paragraph.append(text)
            if paragraph:
                units.append(Unit(UnitKind.TEXT, " ".join(paragraph), page=page.page_number))
    return ParsedSource({}, units)

"""Render plain-text synthetic documents (classification corpus) as PDFs.

Blocks are separated by blank lines; a block whose lines all contain " | " becomes a table, a
short first block becomes a heading. Used by the evaluation suites to push every document type
through real PDF rendering, scanning and OCR.
"""

from __future__ import annotations

import io
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_FONT_SAFE = str.maketrans({"€": "EUR", "₹": "Rs."})  # base-14 fonts lack these glyphs


def render_text_pdf(text: str) -> bytes:
    base = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=base["Normal"], fontSize=9.5, leading=12)
    heading = ParagraphStyle("heading", parent=base["Heading2"], fontSize=14, spaceAfter=4)
    story: list[Flowable] = []
    for index, block in enumerate(text.translate(_FONT_SAFE).split("\n\n")):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if len(lines) >= 2 and all(" | " in line for line in lines):
            rows = [line.split(" | ") for line in lines]
            width = max(len(row) for row in rows)
            rows = [row + [""] * (width - len(row)) for row in rows]
            table = Table(rows, repeatRows=1, hAlign="LEFT")
            style: list[Any] = [
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.grey),
            ]
            table.setStyle(TableStyle(style))
            story.append(table)
        elif index < 2 and len(lines) == 1 and len(lines[0]) <= 60:
            story.append(Paragraph(escape(lines[0]), heading))
        else:
            story += [Paragraph(escape(line), body) for line in lines]
        story.append(Spacer(1, 4 * mm))
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        creator="docintel synthetic generator",
        invariant=1,
    )
    document.build(story)
    return buffer.getvalue()

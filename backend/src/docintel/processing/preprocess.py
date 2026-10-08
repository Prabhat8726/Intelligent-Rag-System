"""Image preprocessing before OCR and clean-up of OCR tokens.

Only steps that measurably help are enabled by default (OCR evaluation suite):
* upscaling low-DPI images (extraction.py),
* dropping ruling-line debris that Tesseract reads as "|", "+" or "_" (token filter below).
Ruling-line removal on the image is available (OCR_REMOVE_RULING_LINES) but off by default:
on synthetic scans it added nothing over the token filter and costs ~0.7 s per page.
Median denoising and autocontrast made synthetic scans worse and are not offered.
"""

from __future__ import annotations

import re

import numpy as np
import numpy.typing as npt
from PIL import Image
from scipy import ndimage

DARK_THRESHOLD = 160  # gray level below which a pixel counts as ink
# Minimum run lengths for ruling lines, in inches: longer than glyph strokes of fonts up to
# ~40 pt, shorter than the grid of a table with a few rows.
VERTICAL_LINE_INCHES = 0.4
HORIZONTAL_LINE_INCHES = 0.6
# Line-shaped OCR tokens: what remains of ruling lines the preprocessing could not remove.
_DASHES = "\\-\u2010-\u2015"
# Tokens made of line characters; a lone hyphen or dash is kept (usually real punctuation).
_LINE_ARTIFACT = re.compile(rf"^(?:[{_DASHES}]*[|¦+_=~][|¦+_=~{_DASHES}]*|[{_DASHES}]{{2,}})$")
# A vertical rule read as a letter: one of these glyphs in a box far narrower than any glyph.
_HAIRLINE_GLYPHS = frozenset("|Il1!")
HAIRLINE_ASPECT = 0.2


def _long_runs(mask: npt.NDArray[np.bool_], length: int, axis: int) -> npt.NDArray[np.bool_]:
    """Pixels belonging to runs of True of at least `length` along `axis` (linear time)."""
    full = ndimage.uniform_filter1d(mask.astype(np.float32), length, axis=axis, mode="constant")
    centers = full > 0.999
    runs: npt.NDArray[np.bool_] = (
        ndimage.maximum_filter1d(centers, length, axis=axis, mode="constant") & mask
    )
    return runs


def remove_ruling_lines(image: Image.Image, dpi: float) -> Image.Image:
    """Whiten long horizontal and vertical lines (table grids, underlines, form rules)."""
    gray = np.asarray(image.convert("L"))
    ink = gray < DARK_THRESHOLD
    # Widen strokes slightly so lines on slightly skewed scans stay connected.
    vertical = _long_runs(
        ndimage.maximum_filter1d(ink, 5, axis=1), max(20, round(dpi * VERTICAL_LINE_INCHES)), 0
    )
    horizontal = _long_runs(
        ndimage.maximum_filter1d(ink, 5, axis=0), max(30, round(dpi * HORIZONTAL_LINE_INCHES)), 1
    )
    lines = (vertical | horizontal) & ink
    if not lines.any():
        return image
    lines = ndimage.binary_dilation(lines, iterations=1) & ink
    cleaned = gray.copy()
    cleaned[lines] = 255
    return Image.fromarray(cleaned, mode="L")


def is_line_artifact(text: str, width: float | None = None, height: float | None = None) -> bool:
    if _LINE_ARTIFACT.match(text):
        return True
    return (
        text in _HAIRLINE_GLYPHS
        and width is not None
        and height is not None
        and height > 0
        and width <= HAIRLINE_ASPECT * height
    )


def strip_line_artifacts(text: str) -> str:
    """Remove ruling-line debris glued to a word: '|Description' -> 'Description'."""
    return text.strip("|¦")

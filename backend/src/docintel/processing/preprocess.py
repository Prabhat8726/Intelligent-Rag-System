"""Image preprocessing before OCR and clean-up of OCR tokens.

Only steps that measurably help are enabled by default (OCR evaluation suite):
* upscaling low-DPI images (extraction.py),
* dropping ruling-line debris that Tesseract reads as "|", "+" or "_" (token filter below),
* deskewing: text rows that drift across a page break table rows apart and cost accuracy.
Ruling-line removal on the image is available (OCR_REMOVE_RULING_LINES) but off by default:
on synthetic scans it added nothing over the token filter and costs ~0.7 s per page.
Median denoising and autocontrast made synthetic scans worse and are not offered.
"""

from __future__ import annotations

import re
from dataclasses import replace

import numpy as np
import numpy.typing as npt
from PIL import Image
from scipy import ndimage

from docintel.processing.ocr import OCRWord

DARK_THRESHOLD = 160  # gray level below which a pixel counts as ink
# Minimum run lengths for ruling lines, in inches: longer than glyph strokes of fonts up to
# ~40 pt, shorter than the grid of a table with a few rows.
VERTICAL_LINE_INCHES = 0.4
HORIZONTAL_LINE_INCHES = 0.6
# Line-shaped OCR tokens: what remains of ruling lines the preprocessing could not remove.
_DASHES = "\\-\u2010-\u2015"
# Tokens made of line characters; a lone hyphen or dash is kept (usually real punctuation).
_RULE_CHARS = r"|¦+_=~\[\]{}"
_LINE_ARTIFACT = re.compile(
    rf"^(?:[{_DASHES}]*[{_RULE_CHARS}][{_RULE_CHARS}{_DASHES}]*|[{_DASHES}]{{2,}})$"
)
# A vertical rule read as a letter: one of these glyphs in a box far narrower than any glyph,
# or narrow and clearly taller than the surrounding text (a grid line crossing the text row).
_HAIRLINE_GLYPHS = frozenset("|Il1!i()[]{}")
HAIRLINE_ASPECT = 0.2
TALL_GLYPH_ASPECT = 0.4
TALL_GLYPH_RATIO = 1.4
# Engine output below this confidence is a guess (Tesseract reports 0 for pure noise).
MIN_WORD_CONFIDENCE = 10.0
# Boxes much flatter than the page's typical word are fragments of horizontal rules.
MIN_HEIGHT_RATIO = 0.3


# Deskew search: +-MAX_SKEW_DEGREES, coarse then fine steps; smaller angles are left alone.
MAX_SKEW_DEGREES = 5.0
MIN_DESKEW_DEGREES = 0.3
_SKEW_SAMPLE_WIDTH = 1000


def estimate_skew_degrees(image: Image.Image) -> float:
    """Text skew by projection profile: the angle whose row histogram of ink is sharpest.

    Rotation by a small angle is approximated by a vertical shear (y' = y - x tan a), so each
    candidate costs one histogram over the ink pixels. Positive = text descends to the right.
    """
    sample = image.convert("L")
    if sample.width > _SKEW_SAMPLE_WIDTH:
        height = max(1, round(sample.height * _SKEW_SAMPLE_WIDTH / sample.width))
        sample = sample.resize((_SKEW_SAMPLE_WIDTH, height), Image.Resampling.BILINEAR)
    ys, xs = np.nonzero(np.asarray(sample) < DARK_THRESHOLD)
    if len(xs) < 200:
        return 0.0
    xs_centered = xs.astype(np.float64) - xs.mean()

    def sharpness(degrees: float) -> float:
        shifted = ys - xs_centered * np.tan(np.radians(degrees))
        histogram = np.bincount(np.round(shifted - shifted.min()).astype(np.int64))
        return float(np.dot(histogram, histogram))

    coarse = np.arange(-MAX_SKEW_DEGREES, MAX_SKEW_DEGREES + 0.01, 0.5)
    best = max(coarse, key=sharpness)
    fine = np.arange(best - 0.5, best + 0.51, 0.05)
    return round(float(max(fine, key=sharpness)), 2)


def deskew(image: Image.Image) -> tuple[Image.Image, float]:
    """Rotate so text rows are horizontal; returns (image, degrees rotated counter-clockwise)."""
    skew = estimate_skew_degrees(image)
    if abs(skew) < MIN_DESKEW_DEGREES:
        return image, 0.0
    # Text descending to the right (positive skew) is undone by a counter-clockwise rotation.
    rotated = image.rotate(skew, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
    return rotated, skew


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


def clean_ocr_words(words: list[OCRWord]) -> list[OCRWord]:
    """Drop OCR tokens that are table rules, noise or fragments rather than text."""
    candidates = []
    for word in words:
        text = strip_line_artifacts(word.text)
        if not text or word.confidence < MIN_WORD_CONFIDENCE:
            continue
        if is_line_artifact(text, word.width, word.height):
            continue
        candidates.append(replace(word, text=text) if text != word.text else word)
    heights = sorted(word.height for word in candidates if len(word.text) > 1)
    if not heights:
        return candidates
    typical = heights[len(heights) // 2]
    return [
        word
        for word in candidates
        if word.height >= MIN_HEIGHT_RATIO * typical
        and not (
            word.text in _HAIRLINE_GLYPHS
            and word.height >= TALL_GLYPH_RATIO * typical
            and word.width <= TALL_GLYPH_ASPECT * word.height
        )
    ]

"""OCR provider (real Tesseract), TSV parsing, skew, preprocessing and token clean-up."""

from __future__ import annotations

import stat
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from docintel.processing.ocr import (
    OCRFailedError,
    OCRResult,
    OCRTimeoutError,
    OCRUnavailableError,
    OCRWord,
    TesseractOCRProvider,
    parse_tsv,
)
from docintel.processing.preprocess import (
    clean_ocr_words,
    deskew,
    estimate_skew_degrees,
    is_line_artifact,
    remove_ruling_lines,
    strip_line_artifacts,
)
from tests.factories.files import text_image

LINES = ["Invoice INV-2026-0042", "Total Due 1,250.00 USD", "Payment terms net 30 days"]


def test_parse_tsv_keeps_words_only() -> None:
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "1\t1\t0\t0\t0\t0\t0\t0\t800\t600\t-1\t",
            "4\t1\t1\t1\t1\t0\t10\t20\t300\t30\t-1\t",
            "5\t1\t1\t1\t1\t1\t10\t20\t120\t30\t96.5\tInvoice",
            "5\t1\t1\t1\t1\t2\t140\t21\t80\t30\t-1\t ",
            "5\t1\t1\t1\t1\t3\t230\t22\t70\t28\t88\t1001",
        ]
    )
    words = parse_tsv(tsv)
    assert [(w.text, w.left, w.confidence, w.line_key) for w in words] == [
        ("Invoice", 10.0, 96.5, (1, 1, 1)),
        ("1001", 230.0, 88.0, (1, 1, 1)),
    ]


def test_skew_is_measured_from_engine_lines() -> None:
    slope = 0.035
    words = [
        OCRWord(f"w{i}", 100 + i * 150, 200 + i * 150 * slope, 100, 30, 95, (1, 1, 1))
        for i in range(6)
    ]
    result = OCRResult(words=words, width=1200, height=1600, latency_ms=1)
    assert result.skew == pytest.approx(slope, abs=1e-6)
    assert OCRResult(words=words[:2], width=1200, height=1600, latency_ms=1).skew == 0.0


async def test_recognizes_rendered_text(tesseract: TesseractOCRProvider) -> None:
    result = await tesseract.recognize(text_image(LINES, dpi=200), dpi=200)
    texts = [word.text for word in result.words]
    for expected in ("Invoice", "INV-2026-0042", "1,250.00", "USD", "net"):
        assert expected in texts
    assert result.mean_confidence is not None
    assert result.mean_confidence > 85
    assert abs(result.skew) < 0.005
    assert result.width > 0
    assert result.latency_ms > 0


async def test_detects_orientation_of_a_rotated_page(tesseract: TesseractOCRProvider) -> None:
    lines = [f"{line} paragraph text for orientation detection" for line in LINES * 4]
    upright = text_image(lines, dpi=200)
    orientation = await tesseract.detect_orientation(upright.rotate(90, expand=True), dpi=200)
    assert orientation is not None
    assert orientation.rotate == 90  # rotate 90 degrees clockwise to undo a 90 degree CCW turn
    assert orientation.confidence > 1


async def test_blank_page_has_no_orientation(tesseract: TesseractOCRProvider) -> None:
    blank = Image.new("L", (800, 1100), 255)
    assert await tesseract.detect_orientation(blank, dpi=100) is None
    result = await tesseract.recognize(blank, dpi=100)
    assert result.words == []
    assert result.mean_confidence is None


async def test_missing_engine_and_language(tmp_path: Path, tesseract: TesseractOCRProvider) -> None:
    with pytest.raises(OCRUnavailableError, match="not found"):
        await TesseractOCRProvider(command=str(tmp_path / "no-such-binary")).verify()
    with pytest.raises(OCRUnavailableError, match="klingon"):
        await TesseractOCRProvider(languages="eng+klingon").verify()
    with pytest.raises(ValueError, match="language"):
        TesseractOCRProvider(languages="eng; rm -rf /")
    assert (await tesseract.verify()).startswith("tesseract")


async def test_slow_engine_is_killed(tmp_path: Path) -> None:
    script = tmp_path / "slow-ocr"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    provider = TesseractOCRProvider(command=str(script), timeout_seconds=0.3)
    with pytest.raises(OCRTimeoutError):
        await provider.recognize(Image.new("L", (10, 10), 255), dpi=72)
    assert OCRTimeoutError.retryable


async def test_missing_tsv_output_is_a_configuration_error(tmp_path: Path) -> None:
    script = tmp_path / "plain-text-ocr"
    script.write_text("#!/bin/sh\necho 'Invoice 1001'\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(OCRUnavailableError, match="TSV"):
        await TesseractOCRProvider(command=str(script)).recognize(Image.new("L", (10, 10)), dpi=72)


async def test_engine_error_is_reported(tmp_path: Path) -> None:
    script = tmp_path / "broken-ocr"
    script.write_text("#!/bin/sh\necho 'Error: cannot read input' >&2\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(OCRFailedError, match="cannot read input"):
        await TesseractOCRProvider(command=str(script)).recognize(Image.new("L", (10, 10)), dpi=72)


@pytest.mark.parametrize(
    ("text", "artifact"),
    [
        ("|", True),
        ("+", True),
        ("__", True),
        ("|+|", True),
        ("--", True),
        ("[", True),
        ("]}", True),
        ("(1)", False),
        ("—", False),
        ("-", False),
        ("I", False),
        ("a-b", False),
        ("Net", False),
    ],
)
def test_line_artifact_tokens(text: str, artifact: bool) -> None:
    assert is_line_artifact(text) is artifact


def test_hairline_glyphs_are_artifacts_only_when_hair_thin() -> None:
    assert is_line_artifact("I", 1, 14)
    assert not is_line_artifact("I", 4, 14)
    assert not is_line_artifact("1", 6, 14)
    assert strip_line_artifacts("|Description|") == "Description"


def test_remove_ruling_lines_keeps_text() -> None:
    image = text_image(["Qty 150"], dpi=200, font_size=12)
    draw = ImageDraw.Draw(image)
    draw.line([(100, 50), (100, 600)], fill=0, width=3)  # vertical rule
    draw.line([(150, 400), (1200, 400)], fill=0, width=3)  # horizontal rule
    cleaned = np.asarray(remove_ruling_lines(image, 200))
    assert cleaned[300, 100] == 255
    assert cleaned[400, 700] == 255
    text_area = np.asarray(image)[150:260, 190:420]
    assert (np.asarray(cleaned)[150:260, 190:420] == text_area).all()


def ocr_word(text: str, *, conf: float = 90, width: float = 60, height: float = 30) -> OCRWord:
    return OCRWord(text, 0, 0, width, height, conf, (1, 1, 1))


def test_clean_ocr_words_drops_noise_but_keeps_text() -> None:
    words = [
        ocr_word("Description"),
        ocr_word("Qty"),
        ocr_word("|Item"),  # rule glued to a word: stripped
        ocr_word("S", conf=0),  # engine guess
        ocr_word("ef", height=2),  # flat fragment of a horizontal rule
        ocr_word("i", width=20, height=70),  # grid line crossing the row, read as "i"
        ocr_word("I", width=8, height=30),  # a real capital I at text height
        ocr_word("[", conf=66),
        ocr_word("5"),
    ]
    assert [word.text for word in clean_ocr_words(words)] == [
        "Description",
        "Qty",
        "Item",
        "I",
        "5",
    ]
    assert clean_ocr_words([]) == []


@pytest.mark.parametrize("angle", [0.0, 0.8, -1.5, 3.0, -4.2])
def test_skew_is_estimated_from_the_image(angle: float) -> None:
    lines = [f"{line} with more words to fill the line" for line in LINES * 4]
    image = text_image(lines, dpi=150).rotate(angle, expand=True, fillcolor=255)
    # PIL rotates counter-clockwise: text then ascends to the right (negative skew).
    assert estimate_skew_degrees(image) == pytest.approx(-angle, abs=0.15)


def test_deskew_levels_the_text_and_skips_tiny_angles() -> None:
    image = text_image([f"{line} with more words" for line in LINES * 4], dpi=150)
    level, applied = deskew(image.rotate(2.5, expand=True, fillcolor=255))
    assert applied == pytest.approx(-2.5, abs=0.15)
    assert abs(estimate_skew_degrees(level)) < 0.15
    untouched, none = deskew(image)
    assert none == 0.0
    assert untouched is image
    blank = Image.new("L", (400, 300), 255)
    assert estimate_skew_degrees(blank) == 0.0

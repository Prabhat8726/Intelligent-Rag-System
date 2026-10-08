"""OCR provider interface and the Tesseract implementation (Module 3, ADR-021).

Tesseract runs as a subprocess (`tesseract stdin stdout ... tsv`): no Python binding to keep in
sync with the binary, real timeouts (the process is killed), and nothing blocks the event loop.
`OMP_THREAD_LIMIT=1` stops Tesseract from spawning a thread per core for every page; pages are
parallelized by the caller instead, which Tesseract's documentation recommends.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Protocol, runtime_checkable

from PIL import Image

# Estimated font size from a word's box height: boxes span ascenders/descenders of the letters
# actually present, about 0.75x the font size on average (calibrated on synthetic pages).
HEIGHT_TO_SIZE = 1 / 0.75
_LANGUAGES = re.compile(r"^[A-Za-z_]+(\+[A-Za-z_]+)*$")
TSV_HEADER = "level\tpage_num\tblock_num"


class OCRError(Exception):
    retryable = False


class OCRUnavailableError(OCRError):
    """The OCR engine or its language data is missing: a deployment problem."""


class OCRTimeoutError(OCRError):
    """A page took longer than the configured limit (the process was killed)."""

    retryable = True


class OCRFailedError(OCRError):
    """The engine exited with an error for this image."""


@dataclass(frozen=True, slots=True)
class OCRWord:
    text: str
    left: float
    top: float
    width: float
    height: float
    confidence: float  # 0-100
    line_key: tuple[int, int, int]  # (block, paragraph, line) as segmented by the engine


@dataclass(frozen=True, slots=True)
class OCRResult:
    words: list[OCRWord]
    width: int
    height: int
    latency_ms: float

    @property
    def mean_confidence(self) -> float | None:
        total = sum(len(word.text) for word in self.words)
        if total == 0:
            return None
        return sum(word.confidence * len(word.text) for word in self.words) / total

    @property
    def quality(self) -> float:
        """Confidence-weighted character count: compares two readings of the same page."""
        return sum(word.confidence * len(word.text) for word in self.words) / 100

    @property
    def skew(self) -> float:
        """Median text slope (dy/dx) over engine lines with enough words; 0 if unknown."""
        lines: dict[tuple[int, int, int], list[OCRWord]] = defaultdict(list)
        for word in self.words:
            lines[word.line_key].append(word)
        slopes: list[float] = []
        for words in lines.values():
            if len(words) < 3:
                continue
            xs = [w.left + w.width / 2 for w in words]
            ys = [w.top + w.height for w in words]
            mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
            spread = sum((x - mean_x) ** 2 for x in xs)
            if spread < (self.width / 10) ** 2:
                continue
            slopes.append(
                sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / spread
            )
        return float(median(slopes)) if slopes else 0.0


@dataclass(frozen=True, slots=True)
class Orientation:
    rotate: int  # clockwise degrees that make the text upright: 0, 90, 180 or 270
    confidence: float


@runtime_checkable
class OCRProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def recognize(self, image: Image.Image, *, dpi: int) -> OCRResult: ...

    async def detect_orientation(self, image: Image.Image, *, dpi: int) -> Orientation | None: ...


def parse_tsv(tsv: str) -> list[OCRWord]:
    words: list[OCRWord] = []
    for row in tsv.splitlines()[1:]:
        columns = row.split("\t")
        if len(columns) != 12 or columns[0] != "5":  # level 5 = word
            continue
        text = columns[11].strip()
        confidence = float(columns[10])
        if not text or confidence < 0:
            continue
        words.append(
            OCRWord(
                text=text,
                left=float(columns[6]),
                top=float(columns[7]),
                width=float(columns[8]),
                height=float(columns[9]),
                confidence=confidence,
                line_key=(int(columns[2]), int(columns[3]), int(columns[4])),
            )
        )
    return words


def _encode(image: Image.Image) -> bytes:
    """Uncompressed PGM/PPM: the fastest format for Tesseract (leptonica) to read from stdin."""
    buffer = io.BytesIO()
    image.save(buffer, format="PPM")
    return buffer.getvalue()


class TesseractOCRProvider:
    def __init__(
        self,
        *,
        command: str = "tesseract",
        languages: str = "eng",
        timeout_seconds: float = 120.0,
        page_segmentation_mode: int = 3,
    ) -> None:
        if not _LANGUAGES.match(languages):
            msg = f"invalid Tesseract language list: {languages!r}"
            raise ValueError(msg)
        self._command = command
        self._languages = languages
        self._timeout = timeout_seconds
        self._psm = page_segmentation_mode

    @property
    def name(self) -> str:
        return "tesseract"

    @property
    def languages(self) -> str:
        return self._languages

    async def _run(self, args: list[str], stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
        environment = {**os.environ, "OMP_THREAD_LIMIT": "1"}
        try:
            process = await asyncio.create_subprocess_exec(
                self._command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except FileNotFoundError as exc:
            msg = f"OCR engine not found: {self._command!r} (install tesseract-ocr)"
            raise OCRUnavailableError(msg) from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), self._timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            msg = f"OCR exceeded {self._timeout:.0f}s"
            raise OCRTimeoutError(msg) from exc
        return process.returncode or 0, stdout, stderr

    async def version(self) -> str:
        code, stdout, stderr = await self._run(["--version"])
        output = (stdout or stderr).decode(errors="replace").splitlines()
        if code != 0 or not output:
            msg = "tesseract --version failed"
            raise OCRUnavailableError(msg)
        return output[0].strip()

    async def available_languages(self) -> list[str]:
        code, stdout, _ = await self._run(["--list-langs"])
        if code != 0:
            msg = "tesseract --list-langs failed"
            raise OCRUnavailableError(msg)
        return [line.strip() for line in stdout.decode().splitlines()[1:] if line.strip()]

    async def verify(self) -> str:
        """Fail fast when the engine, a configured language or TSV output is missing.

        Returns the engine version.
        """
        version = await self.version()
        installed = set(await self.available_languages())
        missing = [lang for lang in self._languages.split("+") if lang not in installed]
        if missing:
            msg = (
                f"Tesseract language data missing: {', '.join(missing)} "
                f"(installed: {sorted(installed)})"
            )
            raise OCRUnavailableError(msg)
        await self.recognize(Image.new("L", (64, 32), 255), dpi=300)  # TSV output works
        return version

    async def recognize(self, image: Image.Image, *, dpi: int) -> OCRResult:
        payload = await asyncio.to_thread(_encode, image)
        args = ["stdin", "stdout", "-l", self._languages, "--psm", str(self._psm)]
        args += ["--dpi", str(dpi), "tsv"]
        started = time.perf_counter()
        code, stdout, stderr = await self._run(args, payload)
        if code != 0:
            detail = stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
            msg = f"tesseract exited with {code}: {detail[0][:200]}"
            raise OCRFailedError(msg)
        tsv = stdout.decode("utf-8", errors="replace")
        if not tsv.startswith(TSV_HEADER):
            # Without its "tsv" config Tesseract prints plain text: every page would look empty.
            msg = "Tesseract did not produce TSV output (tessdata/configs/tsv missing?)"
            raise OCRUnavailableError(msg)
        return OCRResult(
            words=parse_tsv(tsv),
            width=image.width,
            height=image.height,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def detect_orientation(self, image: Image.Image, *, dpi: int) -> Orientation | None:
        """Orientation and script detection (--psm 0). None when the engine cannot tell."""
        payload = await asyncio.to_thread(_encode, image)
        code, stdout, _ = await self._run(
            ["stdin", "stdout", "--psm", "0", "--dpi", str(dpi)], payload
        )
        if code != 0:
            return None
        values = dict(
            line.split(":", 1)
            for line in stdout.decode(errors="replace").splitlines()
            if ":" in line
        )
        try:
            rotate = int(values["Rotate"].strip())
            confidence = float(values["Orientation confidence"].strip())
        except (KeyError, ValueError):
            return None
        return Orientation(rotate % 360, confidence)

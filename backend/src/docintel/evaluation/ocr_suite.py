"""OCR suite (synthetic-noisy): CER / WER / word F1 per degradation, plus preprocessing ablation.

Reference text comes from the native PDF text layer of synthetic documents; each first page is
rendered, degraded and OCR'd through the production extraction path. Both sides are serialized
with the same line builder, so differences measure recognition rather than reading order.
"""

from __future__ import annotations

import random
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PIL import Image

from docintel.documents.validation import FileKind
from docintel.evaluation.common import (
    DEFAULT_PARALLELISM,
    extract_file,
    lines_text,
    png_bytes,
    run_bounded,
)
from docintel.evaluation.metrics import bag_of_words_f1, cer, summary, wer
from docintel.evaluation.report import Report, environment, num, pct
from docintel.processing.content import PageContent
from docintel.processing.extraction import ExtractionOptions
from docintel.processing.inspection import PageMethod
from docintel.processing.layout import analyze_layout
from docintel.processing.native import extract_native_words
from docintel.processing.ocr import TesseractOCRProvider
from docintel.processing.pdf import open_pdf
from docintel.synthetic.degrade import SCAN_PROFILES, degrade, render_pdf_pages
from docintel.synthetic.generator import generate_dataset

DATASET_SEED = 7


@dataclass(frozen=True, slots=True)
class Degradation:
    name: str
    dpi: int
    transform: Callable[[Image.Image, random.Random], Image.Image]
    description: str


def _identity(image: Image.Image, _: random.Random) -> Image.Image:
    return image


DEGRADATIONS = (
    Degradation("clean_300dpi", 300, _identity, "rendered at 300 DPI, no noise"),
    Degradation(
        "light_scan_150dpi",
        150,
        lambda image, rng: degrade(image, SCAN_PROFILES["light"], rng),
        "150 DPI, rotation <=0.8 deg, blur 0.4, noise 4%, JPEG q80 (dataset 'light' scans)",
    ),
    Degradation(
        "heavy_scan_150dpi",
        150,
        lambda image, rng: degrade(image, SCAN_PROFILES["heavy"], rng),
        "150 DPI, rotation <=2 deg, blur 1.0, noise 10%, JPEG q45",
    ),
    Degradation("low_res_100dpi", 100, _identity, "rendered at 100 DPI, no noise"),
    Degradation(
        "skew_3deg",
        200,
        lambda image, _: image.rotate(
            3, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255
        ),
        "200 DPI rotated by 3 degrees",
    ),
    Degradation(
        "sideways_90deg",
        150,
        lambda image, rng: degrade(image, SCAN_PROFILES["light"], rng).rotate(90, expand=True),
        "light scan turned 90 degrees (orientation detection)",
    ),
)
DEFAULT_VARIANT = "default"
ABLATION: dict[str, dict[str, Any]] = {
    "no_upscale": {"upscale_below_dpi": 0},
    "no_deskew": {"deskew": False},
    "remove_ruling_lines": {"remove_ruling_lines": True},
}
ABLATION_DEGRADATIONS = ("light_scan_150dpi", "heavy_scan_150dpi", "low_res_100dpi", "skew_3deg")


def _reference(pdf_path: Path) -> str:
    """Text-layer reference for the first page, serialized like the OCR output."""
    with open_pdf(pdf_path) as document:
        words, width, height = extract_native_words(document[0])
    content = analyze_layout(PageContent(1, width, height, "pt", PageMethod.NATIVE, words))
    return lines_text(content.words, content.lines)


async def run_ocr_suite(output: Path, *, quick: bool = False, languages: str = "eng") -> Report:
    ocr = TesseractOCRProvider(languages=languages, timeout_seconds=180)
    version = await ocr.verify()
    with tempfile.TemporaryDirectory(prefix="docintel-ocr-eval-") as tmp:
        root = Path(tmp)
        manifest = generate_dataset(root / "dataset", seed=DATASET_SEED)
        native = [d for d in manifest["documents"] if d["variant"] == "native"]
        documents = native[:4] if quick else native
        degradations = DEGRADATIONS[:2] + DEGRADATIONS[-1:] if quick else DEGRADATIONS

        jobs: list[tuple[str, str, Path, str, ExtractionOptions]] = []
        base_options = ExtractionOptions()
        for index, entry in enumerate(documents):
            pdf_path = root / "dataset" / entry["file"]
            reference = _reference(pdf_path)
            pdf = pdf_path.read_bytes()
            for spec in degradations:
                rng = random.Random(DATASET_SEED * 1000 + index)  # noqa: S311
                image = spec.transform(render_pdf_pages(pdf, spec.dpi)[0], rng)
                image_path = root / f"{entry['doc_id']}-{spec.name}.png"
                image_path.write_bytes(png_bytes(image, spec.dpi))
                jobs.append((spec.name, DEFAULT_VARIANT, image_path, reference, base_options))
                if spec.name in ABLATION_DEGRADATIONS and not quick:
                    for variant, overrides in ABLATION.items():
                        options = replace(base_options, **overrides)
                        jobs.append((spec.name, variant, image_path, reference, options))

        async def evaluate(job: tuple[str, str, Path, str, ExtractionOptions]) -> dict[str, Any]:
            degradation, variant, path, reference, options = job
            started = time.perf_counter()
            (page,) = await extract_file(path, FileKind.PNG, ocr, options)
            hypothesis = lines_text(page.words, page.lines)
            return {
                "degradation": degradation,
                "variant": variant,
                "cer": cer(reference, hypothesis),
                "wer": wer(reference, hypothesis),
                "word_f1": bag_of_words_f1(reference, hypothesis),
                "confidence": page.ocr_confidence or 0.0,
                "seconds": time.perf_counter() - started,
                "rotated": page.rotation_applied != 0,
            }

        results = await run_bounded(jobs, evaluate, DEFAULT_PARALLELISM)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[(result["degradation"], result["variant"])].append(result)
    metrics: dict[str, dict[str, Any]] = {}
    for (degradation, variant), items in grouped.items():
        metrics.setdefault(degradation, {})[variant] = {
            "pages": len(items),
            "cer": summary([i["cer"] for i in items]),
            "wer": summary([i["wer"] for i in items]),
            "word_f1": summary([i["word_f1"] for i in items]),
            "mean_ocr_confidence": round(sum(i["confidence"] for i in items) / len(items), 2),
            "seconds_per_page": round(sum(i["seconds"] for i in items) / len(items), 3),
            "orientation_corrected_share": round(sum(i["rotated"] for i in items) / len(items), 4),
        }

    def row(degradation: str, variant: str) -> list[str]:
        entry = metrics[degradation][variant]
        return [
            degradation,
            variant,
            str(entry["pages"]),
            pct(entry["cer"]["mean"]),
            pct(entry["wer"]["mean"]),
            num(entry["word_f1"]["mean"]),
            num(entry["mean_ocr_confidence"], 1),
            num(entry["seconds_per_page"], 2),
        ]

    rows = [row(d.name, DEFAULT_VARIANT) for d in degradations]
    ablation_rows = [
        row(degradation, variant)
        for degradation in ABLATION_DEGRADATIONS
        if len(metrics.get(degradation, {})) > 1
        for variant in (DEFAULT_VARIANT, *ABLATION)
        if variant in metrics[degradation]
    ]

    header = ["Degradation", "Variant", "Pages", "CER", "WER", "Word F1", "OCR conf", "s/page"]
    report = Report(
        quick=quick,
        suite="ocr",
        title="OCR evaluation (synthetic-noisy)",
        dataset={
            "name": "synthetic-noisy",
            "source": "synthetic-core generator, first page of each native document",
            "seed": DATASET_SEED,
            "documents": len(documents),
            "degradations": {d.name: d.description for d in degradations},
        },
        config={
            "engine": version,
            "languages": languages,
            "extraction": {
                "ocr_dpi": base_options.ocr_dpi,
                "upscale_below_dpi": base_options.upscale_below_dpi,
                "remove_ruling_lines": base_options.remove_ruling_lines,
                "retry_orientation_below": base_options.retry_orientation_below,
                "deskew": base_options.deskew,
            },
            "quick": quick,
        },
        metrics=metrics,
        environment=environment({"tesseract": version}),
        notes=[
            "Reference text is the PDF text layer of the same page; reference and OCR output "
            "are serialized by the same line builder (top-to-bottom, left-to-right).",
            "CER/WER are edit distances divided by reference length; word F1 is order-insensitive.",
            "Synthetic pages are cleaner than real scans (fonts, layout, no handwriting, no "
            "stamps): these numbers overstate real-world accuracy.",
        ],
        tables=[("Default pipeline", header, rows)]
        + ([("Preprocessing ablation", header, ablation_rows)] if ablation_rows else []),
    )
    report.write(output)
    return report

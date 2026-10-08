"""Classification suite: local model on held-out text, and end to end through PDF + OCR.

A. Text level: the production model (trained on corpus seed 1) on a held-out corpus (other
   seed) as generated, with heavy OCR-style noise, and truncated to 300 characters.
B. End to end, all nine types: held-out corpus documents rendered to PDF; half stay native,
   half are scanned (light profile) and OCR'd; the extracted text is classified.
C. End to end on the synthetic business dataset (purchase orders, invoices, delivery notes;
   native and scanned) - a different generator from the training corpus.
The LLM fallback is not part of these numbers (see notes).
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path
from typing import Any

from docintel.classification.corpus import generate_corpus, ocr_noise
from docintel.classification.model import LocalClassifier
from docintel.db.models import DocumentType
from docintel.documents.validation import FileKind
from docintel.evaluation.common import DEFAULT_PARALLELISM, extract_file, run_bounded, scan_pdf
from docintel.evaluation.metrics import Prediction, classification_report
from docintel.evaluation.report import Report, environment, num, pct
from docintel.processing.extraction import ExtractionOptions
from docintel.processing.ocr import TesseractOCRProvider
from docintel.processing.services import CORPUS_SEED, train_classifier
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.text_render import render_text_pdf

TEXT_SEED = 1001
RENDERED_SEED = 2002
BUSINESS_SEED = 42
LABELS = [label.value for label in DocumentType]
_KINDS = {"pdf": FileKind.PDF, "png": FileKind.PNG, "tiff": FileKind.TIFF}


def _predict(model: LocalClassifier, texts: list[str], truths: list[str]) -> list[Prediction]:
    predictions = model.predict_many(texts)
    return [
        Prediction(truth, p.label.value, p.confidence)
        for truth, p in zip(truths, predictions, strict=True)
    ]


async def run_classification_suite(
    output: Path,
    *,
    threshold: float,
    corpus_per_class: int,
    quick: bool = False,
    languages: str = "eng",
) -> Report:
    model = train_classifier(corpus_per_class)
    ocr = TesseractOCRProvider(languages=languages, timeout_seconds=180)
    version = await ocr.verify()
    options = ExtractionOptions()
    rng = random.Random(TEXT_SEED)  # noqa: S311  (reproducible noise)

    # A. held-out text
    held_out = generate_corpus(seed=TEXT_SEED, per_class=20 if quick else 100)
    truths = [sample.label.value for sample in held_out]
    conditions = {
        "as_generated": [sample.text for sample in held_out],
        "heavy_ocr_noise": [ocr_noise(sample.text, rng, rate=0.08) for sample in held_out],
        "first_300_chars": [sample.text[:300] for sample in held_out],
    }
    results: dict[str, Any] = {
        f"text/{name}": classification_report(_predict(model, texts, truths), LABELS, threshold)
        for name, texts in conditions.items()
    }

    with tempfile.TemporaryDirectory(prefix="docintel-cls-eval-") as tmp:
        root = Path(tmp)
        # B. all types, rendered + scanned
        rendered = generate_corpus(seed=RENDERED_SEED, per_class=2 if quick else 10)
        jobs: list[tuple[str, Path, FileKind, str]] = []
        for index, sample in enumerate(rendered):
            pdf = render_text_pdf(sample.text)
            scanned = index % 2 == 1
            path = root / f"rendered-{index:04d}.pdf"
            path.write_bytes(scan_pdf(pdf, seed=index) if scanned else pdf)
            jobs.append(
                (
                    "rendered/scanned" if scanned else "rendered/native",
                    path,
                    FileKind.PDF,
                    sample.label.value,
                )
            )
        # C. business dataset (different generator)
        manifest = generate_dataset(root / "business", seed=BUSINESS_SEED)
        for entry in manifest["documents"][: 6 if quick else None]:
            path = root / "business" / entry["file"]
            group = "business/scanned" if entry["variant"] == "scanned" else "business/native"
            jobs.append((group, path, _KINDS[path.suffix.lstrip(".")], entry["document_type"]))

        async def evaluate(job: tuple[str, Path, FileKind, str]) -> tuple[str, str, str]:
            group, path, kind, truth = job
            pages = await extract_file(path, kind, ocr, options)
            return group, truth, "\n\n".join(page.text for page in pages)

        extracted = await run_bounded(jobs, evaluate, DEFAULT_PARALLELISM)

    groups: dict[str, tuple[list[str], list[str]]] = {}
    for group, truth, text in extracted:
        texts, group_truths = groups.setdefault(group, ([], []))
        texts.append(text)
        group_truths.append(truth)
    for group in ("rendered/native", "rendered/scanned", "business/native", "business/scanned"):
        if group in groups:
            texts, group_truths = groups[group]
            results[group] = classification_report(
                _predict(model, texts, group_truths), LABELS, threshold
            )
    all_rendered = [
        prediction
        for group in ("rendered/native", "rendered/scanned")
        if group in groups
        for prediction in _predict(model, *groups[group])
    ]
    results["rendered/all"] = classification_report(all_rendered, LABELS, threshold)

    header = [
        "Evaluation set",
        "n",
        "Accuracy",
        "Macro F1",
        "ECE",
        "Auto-accepted",
        "Error in auto bucket",
    ]
    rows = [
        [
            name,
            str(m["n"]),
            pct(m["accuracy"]),
            num(m["macro_f1"]),
            num(m["ece"]),
            pct(m["routing"]["auto_accepted_share"]),
            pct(m["routing"]["error_rate_in_auto_bucket"]),
        ]
        for name, m in results.items()
    ]
    class_stats = results["rendered/all"]["per_class"]
    class_rows = [
        [
            label,
            str(stats["support"]),
            num(stats["precision"]),
            num(stats["recall"]),
            num(stats["f1"]),
        ]
        for label, stats in class_stats.items()
    ]
    confusion = results["rendered/all"]["confusion_matrix"]
    confusion_rows = [
        [truth, *[str(confusion[truth][label]) for label in LABELS]] for truth in LABELS
    ]
    report = Report(
        suite="classification",
        title="Document classification",
        dataset={
            "training": {"corpus_seed": CORPUS_SEED, "per_class": per_class_size(model)},
            "text_held_out": {"seed": TEXT_SEED, "documents": len(held_out)},
            "rendered": {"seed": RENDERED_SEED, "documents": len(rendered), "scanned_share": 0.5},
            "business": {
                "seed": BUSINESS_SEED,
                "documents": len(manifest["documents"][: 6 if quick else None]),
            },
        },
        config={
            "model_version": model.fingerprint,
            "threshold": threshold,
            "engine": version,
            "languages": languages,
            "quick": quick,
        },
        metrics=results,
        environment=environment({"tesseract": version}),
        notes=[
            "Training and evaluation data are synthetic and come from related generators: "
            "near-perfect scores here do NOT predict accuracy on real documents. The business "
            "dataset uses a different generator than the training corpus but is still synthetic.",
            "Auto-accepted = local confidence >= threshold (no review); 'error in auto bucket' "
            "is the share of those that are wrong - the number that matters operationally.",
            "LLM fallback: not measured - no GEMINI_API_KEY in the build environment. Its logic "
            "is covered by tests with a fake provider.",
        ],
        tables=[
            ("Summary", header, rows),
            (
                "Per class (rendered, all nine types)",
                ["Type", "Support", "Precision", "Recall", "F1"],
                class_rows,
            ),
            ("Confusion matrix (rendered; rows = truth)", ["Truth", *LABELS], confusion_rows),
        ],
    )
    report.write(output)
    return report


def per_class_size(model: LocalClassifier) -> int:
    return model.training_size // len(model.labels)

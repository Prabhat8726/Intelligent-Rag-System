"""Synthetic dataset generator (Module 34): files, ground truth and manifest; seeded."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docintel.synthetic.degrade import SCAN_PROFILES, degrade, encode_scan, render_pdf_pages
from docintel.synthetic.model import DocumentTruth
from docintel.synthetic.render import render_pdf
from docintel.synthetic.scenarios import Scenario, build_bundle

GENERATOR_VERSION = "1.0"
DATASET_NAME = "synthetic-core"
SCAN_DPI = 150
MIME_TYPES = {"pdf": "application/pdf", "png": "image/png", "tiff": "image/tiff"}


@dataclass(frozen=True, slots=True)
class GeneratedFile:
    truth: DocumentTruth
    relative_path: str
    sha256: str
    size_bytes: int

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "doc_id": self.truth.doc_id,
            "bundle_id": self.truth.bundle_id,
            "scenario": self.truth.scenario,
            "document_type": self.truth.document_type.value,
            "file": self.relative_path,
            "ground_truth": self.relative_path.rsplit(".", 1)[0] + ".json",
            "mime_type": MIME_TYPES[self.truth.rendering.file_format],
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "variant": self.truth.rendering.variant,
            "defect_codes": [defect["code"] for defect in self.truth.defects],
            "variation_codes": [variation["code"] for variation in self.truth.variations],
        }


def render_document(truth: DocumentTruth, rng: random.Random) -> bytes:
    pdf = render_pdf(truth)
    if truth.rendering.variant != "scanned":
        return pdf
    profile = SCAN_PROFILES[truth.rendering.scan_profile or "light"]
    truth.rendering.dpi = SCAN_DPI
    pages = [degrade(page, profile, rng) for page in render_pdf_pages(pdf, SCAN_DPI)]
    if truth.rendering.file_format == "png":
        pages = pages[:1]  # PNG holds a single page
    return encode_scan(pages, truth.rendering.file_format, SCAN_DPI)


def generate_dataset(
    output_dir: Path,
    *,
    seed: int = 42,
    bundles_per_scenario: int = 1,
    scenarios: list[Scenario] | None = None,
) -> dict[str, Any]:
    """Write documents, ground-truth JSON and manifest.json under `output_dir`."""
    rng = random.Random(seed)  # noqa: S311  (reproducible test data, not security)
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = scenarios or list(Scenario)
    generated: list[GeneratedFile] = []
    bundle_index = 1
    for scenario in selected:
        for _ in range(bundles_per_scenario):
            for truth in build_bundle(scenario, bundle_index, rng):
                content = render_document(truth, rng)
                extension = truth.rendering.file_format
                relative = f"{truth.bundle_id}/{truth.doc_id}.{extension}"
                path = output_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.with_suffix(".json").write_text(
                    json.dumps(truth.to_json(), indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                generated.append(
                    GeneratedFile(
                        truth=truth,
                        relative_path=relative,
                        sha256=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content),
                    )
                )
            bundle_index += 1

    manifest = {
        "dataset": DATASET_NAME,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "bundles_per_scenario": bundles_per_scenario,
        "scenarios": [scenario.value for scenario in selected],
        "document_count": len(generated),
        "documents": [item.manifest_entry() for item in generated],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest

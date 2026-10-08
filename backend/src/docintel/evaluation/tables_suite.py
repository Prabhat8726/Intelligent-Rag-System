"""Table suite: line-item tables against generator ground truth, native and scanned.

For every synthetic document the line-item table (header containing "Description") is located
among the detected tables and compared with the true line items: table found, row count exact,
row precision/recall (rows matched by SKU) and cell accuracy per column on matched rows.
Native PDFs are also re-rendered as light scans so both extraction paths are measured.
"""

from __future__ import annotations

import json
import tempfile
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from docintel.documents.validation import FileKind
from docintel.evaluation.common import DEFAULT_PARALLELISM, extract_file, run_bounded, scan_pdf
from docintel.evaluation.report import Report, environment, num, pct
from docintel.processing.content import DocumentTable
from docintel.processing.extraction import ExtractionOptions
from docintel.processing.ocr import TesseractOCRProvider
from docintel.processing.tables import stitch_tables
from docintel.synthetic.catalog import LOCALES, VENDORS
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.render import format_amount, format_quantity
from docintel.synthetic.scenarios import Scenario

DATASET_SEED = 11
COLUMNS = ("number", "sku", "description", "quantity", "unit", "unit_price", "amount")
# Rows are paired with ground truth by SKU similarity, so one misread character (0 vs O) is
# scored as a cell error instead of a missing row.
SKU_MATCH_RATIO = 75
_KINDS = {"pdf": FileKind.PDF, "png": FileKind.PNG, "tiff": FileKind.TIFF}


def _expected_rows(truth: dict[str, Any]) -> list[list[str]]:
    vendor = next(v for v in VENDORS if v.code == truth["fields"]["vendor_code"])
    locale = LOCALES[vendor.country]
    rows = []
    for item in truth["line_items"]:
        row = [
            str(item["line_number"]),
            item["sku"],
            item["description"],
            format_quantity(Decimal(item["quantity"])),
            item["unit"],
        ]
        if "unit_price" in item:
            row += [
                format_amount(Decimal(item["unit_price"]), locale),
                format_amount(Decimal(item["line_total"]), locale),
            ]
        rows.append(row)
    return rows


def _line_item_table(tables: list[DocumentTable]) -> DocumentTable | None:
    for table in tables:
        if any("description" in cell.casefold() for cell in table.header):
            return table
    return None


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def score_table(table: DocumentTable | None, expected: list[list[str]]) -> dict[str, Any]:
    if table is None:
        return {
            "found": False,
            "expected_rows": len(expected),
            "detected_rows": 0,
            "matched": 0,
            "cells": {},
        }
    detected = [row.cells for row in table.rows]
    unmatched = list(range(len(detected)))
    matched_pairs: list[tuple[list[str], list[str]]] = []
    for row in expected:
        sku = _normalize(row[1])
        candidates = [
            (fuzz.ratio(sku, _normalize(detected[position][1])), position)
            for position in unmatched
            if len(detected[position]) > 1
        ]
        if candidates:
            similarity, position = max(candidates)
            if similarity >= SKU_MATCH_RATIO:
                matched_pairs.append((row, detected[position]))
                unmatched.remove(position)
    cells: dict[str, list[bool]] = defaultdict(list)
    for truth_row, detected_row in matched_pairs:
        for index, value in enumerate(truth_row):
            got = detected_row[index] if index < len(detected_row) else ""
            cells[COLUMNS[index]].append(_normalize(got) == _normalize(value))
    return {
        "found": True,
        "expected_rows": len(expected),
        "detected_rows": len(detected),
        "matched": len(matched_pairs),
        "cells": dict(cells),
    }


def _aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    found = [s for s in scores if s["found"]]
    expected = sum(s["expected_rows"] for s in scores)
    detected = sum(s["detected_rows"] for s in scores)
    matched = sum(s["matched"] for s in scores)
    precision = matched / detected if detected else 0.0
    recall = matched / expected if expected else 0.0
    columns: dict[str, list[bool]] = defaultdict(list)
    for score in found:
        for column, values in score["cells"].items():
            columns[column].extend(values)
    all_cells = [value for values in columns.values() for value in values]
    return {
        "documents": len(scores),
        "table_found_rate": round(len(found) / len(scores), 4) if scores else 0.0,
        "row_count_exact_rate": round(
            sum(1 for s in scores if s["found"] and s["detected_rows"] == s["expected_rows"])
            / len(scores),
            4,
        )
        if scores
        else 0.0,
        "row_precision": round(precision, 4),
        "row_recall": round(recall, 4),
        "row_f1": round(2 * precision * recall / (precision + recall), 4)
        if precision + recall
        else 0.0,
        "cell_accuracy": round(sum(all_cells) / len(all_cells), 4) if all_cells else 0.0,
        "cell_accuracy_by_column": {
            column: round(sum(values) / len(values), 4)
            for column, values in sorted(columns.items(), key=lambda item: COLUMNS.index(item[0]))
        },
    }


async def run_tables_suite(output: Path, *, quick: bool = False, languages: str = "eng") -> Report:
    ocr = TesseractOCRProvider(languages=languages, timeout_seconds=180)
    version = await ocr.verify()
    options = ExtractionOptions()
    with tempfile.TemporaryDirectory(prefix="docintel-tables-eval-") as tmp:
        root = Path(tmp)
        scenarios = [Scenario.CLEAN_MATCH, Scenario.SCANNED_DOCUMENTS] if quick else None
        manifest = generate_dataset(
            root / "dataset",
            seed=DATASET_SEED,
            bundles_per_scenario=1 if quick else 2,
            scenarios=scenarios,
        )
        jobs: list[tuple[str, Path, FileKind, list[list[str]], str]] = []
        for entry in manifest["documents"]:
            path = root / "dataset" / entry["file"]
            truth = json.loads((root / "dataset" / entry["ground_truth"]).read_text())
            expected = _expected_rows(truth)
            template = truth["rendering"]["template"]
            kind = _KINDS[path.suffix.lstrip(".")]
            if entry["variant"] == "native":
                jobs.append(("native", path, kind, expected, template))
                scanned = path.with_name(path.stem + "-scan.pdf")
                scanned.write_bytes(scan_pdf(path.read_bytes(), seed=len(jobs)))
                jobs.append(("scanned (re-rendered)", scanned, FileKind.PDF, expected, template))
            else:
                jobs.append(("scanned (dataset)", path, kind, expected, template))

        async def evaluate(
            job: tuple[str, Path, FileKind, list[list[str]], str],
        ) -> tuple[str, str, dict[str, Any]]:
            method, path, kind, expected, template = job
            pages = await extract_file(path, kind, ocr, options)
            return method, template, score_table(_line_item_table(stitch_tables(pages)), expected)

        results = await run_bounded(jobs, evaluate, DEFAULT_PARALLELISM)

    by_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_template: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for method, template, score in results:
        by_method[method].append(score)
        by_template[(method, template)].append(score)
    metrics = {
        "by_method": {method: _aggregate(scores) for method, scores in sorted(by_method.items())},
        "by_method_and_template": {
            f"{method} / {template}": _aggregate(scores)
            for (method, template), scores in sorted(by_template.items())
        },
    }
    header = ["Input", "Docs", "Table found", "Rows exact", "Row P", "Row R", "Cell accuracy"]

    def rows_for(data: dict[str, dict[str, Any]]) -> list[list[str]]:
        return [
            [
                name,
                str(m["documents"]),
                pct(m["table_found_rate"]),
                pct(m["row_count_exact_rate"]),
                num(m["row_precision"]),
                num(m["row_recall"]),
                pct(m["cell_accuracy"]),
            ]
            for name, m in data.items()
        ]

    column_header = ["Input", *COLUMNS]
    column_rows = [
        [name, *[pct(m["cell_accuracy_by_column"].get(column)) for column in COLUMNS]]
        for name, m in metrics["by_method"].items()
    ]
    report = Report(
        suite="tables",
        title="Line-item table extraction",
        dataset={
            "name": "synthetic-core",
            "seed": DATASET_SEED,
            "documents": len(manifest["documents"]),
            "bundles_per_scenario": manifest["bundles_per_scenario"],
            "scanned_rerender": "light scan profile at 150 DPI",
        },
        config={"engine": version, "languages": languages, "quick": quick},
        metrics=metrics,
        environment=environment({"tesseract": version}),
        notes=[
            f"Rows are paired with ground truth by SKU similarity (ratio >= {SKU_MATCH_RATIO}); "
            "cell accuracy is exact match after whitespace/case normalization, on paired rows.",
            "Templates: classic (grid lines), modern (header rule, banded rows), compact "
            "(row rules, stacked header).",
            "Synthetic layouts are regular; real-world tables (merged cells, rotated headers, "
            "handwriting) will score lower.",
        ],
        tables=[
            ("By input", header, rows_for(metrics["by_method"])),
            ("By input and template", header, rows_for(metrics["by_method_and_template"])),
            ("Cell accuracy by column", column_header, column_rows),
        ],
    )
    report.write(output)
    return report

"""Extraction suite: structured fields against generator ground truth (Modules 6-8, 25, 26).

Every purchase order, delivery note and invoice of synthetic-core is extracted exactly as the
worker does (text layer / OCR -> layout -> tables -> structured extraction), natively and
re-rendered as a light scan, plus the dataset's own scans. The document type comes from the
ground truth so classification errors do not leak into these numbers. Vendor resolution uses
the demo vendor master (canonical names and tax IDs only, as `docintel seed` creates it).

Measured:
* per field: exact match (printed text), normalized match (typed value), precision / recall /
  F1 with explicit nulls (a wrong value counts as both a false positive and a false negative);
* line items: rows paired by SKU, row precision / recall, normalized cell accuracy per column;
* normalization: vendor resolution to the canonical vendor, dates in every printed format;
* consistency checks: printed arithmetic errors flagged, false alarms on correct documents;
* routing: share auto-accepted and the error rate inside that bucket;
* calibration (Phase 10): field and cell confidence against accuracy (reliability, ECE) and a
  sweep of the auto-accept threshold, chosen on this dataset by a fixed rule and reported on a
  held-out dataset generated with another seed (`evaluation.calibration`).
The LLM extractor is not measured here (no model in the build environment): layout only.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from docintel.db.models import DocumentType, ReviewReason
from docintel.documents.validation import FileKind
from docintel.evaluation.calibration import (
    DESIGN_FLOOR,
    DocumentOutcome,
    calibration_error,
    choose_threshold,
    overconfident_share,
    reliability,
    threshold_sweep,
)
from docintel.evaluation.common import DEFAULT_PARALLELISM, extract_file, run_bounded, scan_pdf
from docintel.evaluation.report import Report, environment, num, pct
from docintel.fields.confidence import ReviewLevel
from docintel.fields.normalize import organization_key
from docintel.fields.service import (
    ExtractionOutcome,
    ExtractionPolicy,
    ExtractionRequest,
    FieldExtractionService,
    ResolvedField,
)
from docintel.fields.vendors import StaticVendorDirectory, VendorRecord, tax_id_key
from docintel.processing.extraction import ExtractionOptions
from docintel.processing.ocr import TesseractOCRProvider
from docintel.processing.tables import stitch_tables
from docintel.synthetic.catalog import LOCALES, VENDORS
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.render import format_money, format_quantity
from docintel.synthetic.scenarios import Scenario

DATASET_SEED = 31
HELD_OUT_SEED = 131  # calibration only: never used to choose anything
SKU_MATCH_RATIO = 75
_KINDS = {"pdf": FileKind.PDF, "png": FileKind.PNG, "tiff": FileKind.TIFF}
LINE_COLUMNS = ("line_number", "sku", "description", "quantity", "unit", "unit_price", "amount")

# schema field -> ground-truth key, per document type
FIELD_TRUTH: dict[DocumentType, dict[str, str]] = {
    DocumentType.INVOICE: {
        "vendor_name": "vendor_name",
        "vendor_tax_id": "vendor_tax_id",
        "invoice_number": "number",
        "invoice_date": "issue_date",
        "due_date": "due_date",
        "purchase_order_number": "purchase_order_number",
        "buyer_name": "buyer_name",
        "currency": "currency",
        "payment_terms_days": "payment_terms_days",
        "subtotal": "subtotal",
        "tax_amount": "tax",
        "tax_rate": "tax_rate",
        "total": "total",
    },
    DocumentType.PURCHASE_ORDER: {
        "po_number": "number",
        "po_date": "issue_date",
        "vendor_name": "vendor_name",
        "vendor_tax_id": "vendor_tax_id",
        "buyer_name": "buyer_name",
        "currency": "currency",
        "payment_terms_days": "payment_terms_days",
        "subtotal": "subtotal",
        "tax_amount": "tax",
        "tax_rate": "tax_rate",
        "total": "total",
    },
    DocumentType.DELIVERY_NOTE: {
        "delivery_note_number": "number",
        "delivery_date": "issue_date",
        "vendor_name": "vendor_name",
        "purchase_order_number": "purchase_order_number",
        "recipient_name": "buyer_name",
    },
}
# Fields grouped for reporting under one name across document types.
FIELD_GROUP = {
    "invoice_number": "document number",
    "po_number": "document number",
    "delivery_note_number": "document number",
    "invoice_date": "document date",
    "po_date": "document date",
    "delivery_date": "document date",
    "recipient_name": "buyer_name",
}


def demo_vendor_records() -> list[VendorRecord]:
    return [
        VendorRecord(
            uuid.UUID(int=index + 1),
            vendor.name,
            organization_key(vendor.name),
            (),
            tax_id_key(vendor.tax_id),
            LOCALES[vendor.country].currency,
            vendor.payment_terms_days,
        )
        for index, vendor in enumerate(VENDORS)
    ]


def _vendor(code: str) -> Any:
    return next(v for v in VENDORS if v.code == code)


def expected_values(truth: dict[str, Any]) -> dict[str, tuple[str | None, Any]]:
    """field key -> (printed text, typed value) as the generator printed it."""
    fields = truth["fields"]
    vendor = _vendor(fields["vendor_code"])
    locale = LOCALES[vendor.country]
    date_format = truth["rendering"]["date_format"]

    def printed_date(value: str | None) -> tuple[str | None, str | None]:
        if value is None:
            return None, None
        return date.fromisoformat(value).strftime(date_format), value

    def money(value: str | None) -> tuple[str | None, Decimal | None]:
        if value is None:
            return None, None
        return format_money(Decimal(value), locale), Decimal(value)

    rate = fields.get("tax_rate")
    terms = fields.get("payment_terms_days")
    return {
        "number": (fields["number"], fields["number"].replace(" ", "").upper()),
        "vendor_name": (fields["vendor_name"], fields["vendor_name_canonical"]),
        "vendor_tax_id": (vendor.tax_id, tax_id_key(vendor.tax_id)),
        "issue_date": printed_date(fields["issue_date"]),
        "due_date": printed_date(fields.get("due_date")),
        "purchase_order_number": (
            fields.get("purchase_order_number"),
            fields.get("purchase_order_number"),
        ),
        "buyer_name": (fields["buyer_name"], organization_key(fields["buyer_name"])),
        "currency": (fields["currency"], fields["currency"]),
        "payment_terms_days": (
            None if terms is None else f"Net {terms} days",
            terms,
        ),
        "subtotal": money(fields.get("subtotal")),
        "tax": money(fields.get("tax")),
        "tax_rate": (
            None if rate is None else f"{format_quantity(Decimal(rate) * 100)}%",
            None if rate is None else Decimal(rate),
        ),
        "total": money(fields.get("total")),
    }


def _typed_match(field: str, item: ResolvedField, expected: Any) -> bool:
    value = item.value
    if value is None:
        return False
    if field == "vendor_name":
        vendor = (item.normalized or {}).get("vendor") or {}
        return bool(vendor.get("canonical_name") == expected)
    if field in ("buyer_name", "recipient_name"):
        return bool(organization_key(str(value)) == expected)
    if field == "vendor_tax_id":
        return bool(tax_id_key(str(value)) == expected)
    if isinstance(expected, Decimal):
        return Decimal(str(value)) == expected
    return bool(value == expected)


def _collapse(text: str | None) -> str:
    return " ".join((text or "").split())


def score_document(
    doc_type: DocumentType, truth: dict[str, Any], outcome: ExtractionOutcome
) -> dict[str, Any]:
    expected = expected_values(truth)
    by_path = {item.path: item for item in outcome.fields}
    fields: dict[str, dict[str, Any]] = {}
    for name, truth_key in FIELD_TRUTH[doc_type].items():
        printed, typed = expected[truth_key]
        item = by_path.get(name)
        predicted = item is not None and item.found and item.value is not None
        exact = bool(
            item and printed is not None and _collapse(item.original_value) == _collapse(printed)
        )
        correct = bool(item and typed is not None and _typed_match(name, item, typed))
        fields[name] = {
            "truth": typed is not None,
            "predicted": predicted,
            "exact": exact,
            "correct": correct,
            "confidence": item.confidence if item else 0.0,
        }

    # Line items, paired by SKU (one misread character is a cell error, not a missing row).
    rows: dict[int, dict[str, ResolvedField]] = defaultdict(dict)
    for item in outcome.fields:
        if item.group == "line_items" and item.row_index is not None:
            rows[item.row_index][item.name] = item
    predicted_rows = [rows[index] for index in sorted(rows)]
    unmatched = list(range(len(predicted_rows)))
    cells: dict[str, list[bool]] = defaultdict(list)
    cell_confidence: list[tuple[float, bool]] = []
    matched = 0
    row_errors = False
    for line in truth["line_items"]:
        candidates = [
            (fuzz.ratio(line["sku"], predicted_rows[i]["sku"].original_value or ""), i)
            for i in unmatched
            if "sku" in predicted_rows[i]
        ]
        best = max(candidates, default=None)
        if best is None or best[0] < SKU_MATCH_RATIO:
            row_errors = True
            continue
        unmatched.remove(best[1])
        matched += 1
        got = predicted_rows[best[1]]
        wanted: dict[str, Any] = {
            "line_number": line["line_number"],
            "sku": line["sku"],
            "description": line["description"],
            "quantity": Decimal(line["quantity"]),
            "unit": line["unit"],
        }
        if "unit_price" in line:
            wanted["unit_price"] = Decimal(line["unit_price"])
            wanted["amount"] = Decimal(line["line_total"])
        for column, value in wanted.items():
            cell = got.get(column)
            ok = (
                cell is not None
                and cell.value is not None
                and (
                    Decimal(str(cell.value)) == value
                    if isinstance(value, Decimal)
                    else str(cell.value).strip() == str(value)
                )
            )
            cells[column].append(ok)
            if cell is not None and cell.value is not None:
                cell_confidence.append((cell.confidence, ok))
            row_errors = row_errors or not ok
    row_errors = row_errors or bool(unmatched)

    required = [name for name in outcome.schema.required_fields if name in fields]
    field_errors = any(
        fields[name]["truth"] != fields[name]["predicted"]
        or (fields[name]["truth"] and not fields[name]["correct"])
        for name in fields
    )
    return {
        "fields": fields,
        "rows": {
            "expected": len(truth["line_items"]),
            "predicted": len(predicted_rows),
            "matched": matched,
        },
        "cells": dict(cells),
        "cell_confidence": cell_confidence,
        "required_ok": all(fields[name]["correct"] for name in required),
        "any_error": field_errors or row_errors,
        "level": outcome.scoring.level.value,
        "inconsistent": ReviewReason.EXTRACTION_INCONSISTENT in outcome.scoring.reasons,
        "confidence": outcome.scoring.confidence,
    }


def _prf(records: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(1 for r in records if r["truth"] and r["predicted"] and r["correct"])
    wrong = sum(1 for r in records if r["truth"] and r["predicted"] and not r["correct"])
    fp = wrong + sum(1 for r in records if not r["truth"] and r["predicted"])
    fn = wrong + sum(1 for r in records if r["truth"] and not r["predicted"])
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    with_truth = [r for r in records if r["truth"]]
    return {
        "n": len(records),
        "with_value": len(with_truth),
        "exact_match": round(sum(r["exact"] for r in with_truth) / len(with_truth), 4)
        if with_truth
        else None,
        "normalized_match": round(sum(r["correct"] for r in with_truth) / len(with_truth), 4)
        if with_truth
        else None,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    by_field: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for score in scores:
        for name, record in score["fields"].items():
            by_field[FIELD_GROUP.get(name, name)].append(record)
    all_records = [record for records in by_field.values() for record in records]
    expected = sum(s["rows"]["expected"] for s in scores)
    predicted = sum(s["rows"]["predicted"] for s in scores)
    matched = sum(s["rows"]["matched"] for s in scores)
    precision = matched / predicted if predicted else 0.0
    recall = matched / expected if expected else 0.0
    columns: dict[str, list[bool]] = defaultdict(list)
    for score in scores:
        for column, values in score["cells"].items():
            columns[column].extend(values)
    cells = [value for values in columns.values() for value in values]
    auto = [s for s in scores if s["level"] == ReviewLevel.AUTO.value]
    high_fields = [
        r
        for r in all_records
        if r["predicted"] and r["confidence"] >= ExtractionPolicy().thresholds.high
    ]
    return {
        "documents": len(scores),
        "fields": {name: _prf(records) for name, records in sorted(by_field.items())},
        "all_fields": _prf(all_records),
        "required_fields_correct_rate": round(
            sum(1 for s in scores if s["required_ok"]) / len(scores), 4
        )
        if scores
        else None,
        "documents_fully_correct_rate": round(
            sum(1 for s in scores if not s["any_error"]) / len(scores), 4
        )
        if scores
        else None,
        "line_items": {
            "row_precision": round(precision, 4),
            "row_recall": round(recall, 4),
            "row_f1": round(2 * precision * recall / (precision + recall), 4)
            if precision + recall
            else 0.0,
            "cell_accuracy": round(sum(cells) / len(cells), 4) if cells else None,
            "cell_accuracy_by_column": {
                column: round(sum(values) / len(values), 4)
                for column, values in sorted(
                    columns.items(), key=lambda kv: LINE_COLUMNS.index(kv[0])
                )
            },
        },
        "routing": {
            "auto_share": round(len(auto) / len(scores), 4) if scores else None,
            "error_rate_in_auto": round(sum(1 for s in auto if s["any_error"]) / len(auto), 4)
            if auto
            else None,
            "levels": {
                level.value: sum(1 for s in scores if s["level"] == level.value)
                for level in ReviewLevel
            },
            "high_confidence_fields": len(high_fields),
            "high_confidence_field_error_rate": round(
                sum(1 for r in high_fields if not r["correct"]) / len(high_fields), 4
            )
            if high_fields
            else None,
        },
    }


Scored = list[tuple[str, dict[str, Any], dict[str, Any]]]  # (input, truth, score) per document


def _confidence_pairs(values: list[tuple[float, bool]]) -> dict[str, Any]:
    return {
        "n": len(values),
        "ece": calibration_error(values),
        "overconfident_share": overconfident_share(values),
        "bins": reliability(values),
    }


def calibration(results: Scored) -> dict[str, Any]:
    """Field and cell confidence against accuracy, and the auto-accept threshold sweep, for all
    inputs and for native and scanned inputs apart."""
    groups = {
        "all": results,
        "native": [row for row in results if row[0] == "native"],
        "scanned": [row for row in results if row[0] != "native"],
    }
    out: dict[str, Any] = {}
    for name, rows in groups.items():
        scores = [score for _, _, score in rows]
        fields = [
            (float(record["confidence"]), bool(record["truth"] and record["correct"]))
            for score in scores
            for record in score["fields"].values()
            if record["predicted"]
        ]
        cells = [(float(c), bool(ok)) for score in scores for c, ok in score["cell_confidence"]]
        out[name] = {
            "documents": len(scores),
            "fields": _confidence_pairs(fields),
            "cells": _confidence_pairs(cells),
            "sweep": threshold_sweep(
                [
                    DocumentOutcome(score["confidence"], score["inconsistent"], score["any_error"])
                    for score in scores
                ]
            ),
        }
    return out


def _errors(row: dict[str, Any]) -> str:
    """Errors among auto-accepted documents with the 95% bound; a note when none are."""
    if not row["auto"]:
        return "none auto-accepted"
    return f"{row['errors']} (≤ {pct(row['error_upper_95'])})"


def _sweep_rows(
    development: list[dict[str, Any]],
    held_out: list[dict[str, Any]],
    *,
    current: float,
    chosen: float | None,
) -> list[list[str]]:
    rows = []
    for dev, test in zip(development, held_out, strict=True):
        threshold = dev["threshold"]
        marks = [
            label
            for label, value in (("current", current), ("chosen", chosen))
            if value is not None and abs(threshold - value) < 1e-9
        ]
        rows.append(
            [
                f"{threshold:.2f}" + (f" ({', '.join(marks)})" if marks else ""),
                f"{dev['auto']} of {dev['documents']} ({pct(dev['auto_share'])})",
                _errors(dev),
                f"{test['auto']} of {test['documents']} ({pct(test['auto_share'])})",
                _errors(test),
            ]
        )
    return rows


def _reliability_rows(fields: list[dict[str, Any]], cells: list[dict[str, Any]]) -> list[list[str]]:
    by_low: dict[float, dict[str, Any]] = defaultdict(dict)
    for kind, rows in (("fields", fields), ("cells", cells)):
        for row in rows:
            by_low[row["low"]][kind] = row
    table = []
    for low in sorted(by_low):
        entry = by_low[low]
        high = (entry.get("fields") or entry["cells"])["high"]
        cells_out = []
        for kind in ("fields", "cells"):
            found = entry.get(kind)
            cells_out += (
                [str(found["n"]), num(found["mean_confidence"]), pct(found["accuracy"])]
                if found
                else ["0", "", ""]
            )
        table.append([f"{low:.1f}-{high:.1f}", *cells_out])
    return table


async def _extract_dataset(
    root: Path,
    *,
    seed: int,
    bundles: int,
    scenarios: list[Scenario] | None,
    ocr: TesseractOCRProvider,
    service: FieldExtractionService,
) -> tuple[dict[str, Any], Scored]:
    """Generate a dataset and extract every document natively and re-rendered as a scan."""
    options = ExtractionOptions()
    with tempfile.TemporaryDirectory(prefix="docintel-extraction-eval-", dir=root) as tmp:
        folder = Path(tmp)
        manifest = generate_dataset(
            folder / "dataset", seed=seed, bundles_per_scenario=bundles, scenarios=scenarios
        )
        jobs: list[tuple[str, Path, FileKind, dict[str, Any]]] = []
        for entry in manifest["documents"]:
            path = folder / "dataset" / entry["file"]
            truth = json.loads((folder / "dataset" / entry["ground_truth"]).read_text())
            kind = _KINDS[path.suffix.lstrip(".")]
            if entry["variant"] == "native":
                jobs.append(("native", path, kind, truth))
                scanned = path.with_name(path.stem + "-scan.pdf")
                scanned.write_bytes(scan_pdf(path.read_bytes(), seed=len(jobs)))
                jobs.append(("scanned (re-rendered)", scanned, FileKind.PDF, truth))
            else:
                jobs.append(("scanned (dataset)", path, kind, truth))

        async def evaluate(
            job: tuple[str, Path, FileKind, dict[str, Any]],
        ) -> tuple[str, dict[str, Any], dict[str, Any]]:
            method, path, kind, truth = job
            doc_type = DocumentType(truth["document_type"])
            pages = await extract_file(path, kind, ocr, options)
            outcome = await service.extract(
                ExtractionRequest(doc_type, pages, stitch_tables(pages))
            )
            if outcome is None:  # pragma: no cover - every evaluated type has a schema
                msg = f"no extraction schema for {doc_type}"
                raise RuntimeError(msg)
            return method, truth, score_document(doc_type, truth, outcome)

        results = await run_bounded(jobs, evaluate, DEFAULT_PARALLELISM)
    return manifest, results


async def run_extraction_suite(
    output: Path, *, quick: bool = False, languages: str = "eng"
) -> Report:
    ocr = TesseractOCRProvider(languages=languages, timeout_seconds=180)
    version = await ocr.verify()
    service = FieldExtractionService(
        policy=ExtractionPolicy(llm_mode="never"),
        vendors=StaticVendorDirectory(demo_vendor_records()),
    )
    scenarios = (
        [Scenario.CLEAN_MATCH, Scenario.TOTAL_ARITHMETIC_ERROR, Scenario.VENDOR_NAME_VARIANT]
        if quick
        else None
    )
    with tempfile.TemporaryDirectory(prefix="docintel-extraction-eval-") as tmp:
        manifest, results = await _extract_dataset(
            Path(tmp),
            seed=DATASET_SEED,
            bundles=1 if quick else 2,
            scenarios=scenarios,
            ocr=ocr,
            service=service,
        )
        held_out_manifest, held_out = await _extract_dataset(
            Path(tmp), seed=HELD_OUT_SEED, bundles=1, scenarios=scenarios, ocr=ocr, service=service
        )

    by_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    arithmetic: dict[str, list[bool]] = defaultdict(list)
    variants: dict[str, list[bool]] = defaultdict(list)
    for method, truth, score in results:
        by_method[method].append(score)
        by_type[f"{method} / {truth['document_type']}"].append(score)
        has_error = any(d["code"] == "TOTAL_MISMATCH" for d in truth["defects"])
        # On a correctly printed document a failed check means a value was misread: the flag
        # is right (the misread is caught), but it sends a correct document to review.
        arithmetic[
            "printed total wrong" if has_error else "printed document consistent (flag = misread)"
        ].append(score["inconsistent"])
        for variation in truth["variations"]:
            field = "vendor_name" if variation["code"] == "VENDOR_NAME_VARIANT" else None
            if variation["code"] == "DATE_FORMAT_VARIANT":
                field = {"INVOICE": "invoice_date", "PURCHASE_ORDER": "po_date"}.get(
                    truth["document_type"], "delivery_date"
                )
            if field and field in score["fields"]:
                variants[f"{method} / {variation['code']}"].append(
                    score["fields"][field]["correct"]
                )

    development, held = calibration(results), calibration(held_out)
    current = ExtractionPolicy().thresholds.high
    chosen = choose_threshold(development["all"]["sweep"])
    metrics: dict[str, Any] = {
        "calibration": {
            "development": development,
            "held_out": held,
            "design_floor": DESIGN_FLOOR,
            "current_threshold": current,
            "chosen_threshold": chosen,
        },
        "by_method": {name: aggregate(scores) for name, scores in sorted(by_method.items())},
        "by_method_and_type": {name: aggregate(scores) for name, scores in sorted(by_type.items())},
        "consistency_checks": {
            name: {"documents": len(values), "rate": round(sum(values) / len(values), 4)}
            for name, values in sorted(arithmetic.items())
        },
        "normalization_variants": {
            name: {"documents": len(values), "correct": round(sum(values) / len(values), 4)}
            for name, values in sorted(variants.items())
        },
    }

    summary_header = [
        "Input",
        "Docs",
        "Field exact",
        "Field normalized",
        "Field P",
        "Field R",
        "Field F1",
        "Required all correct",
        "Docs fully correct",
    ]
    summary_rows = [
        [
            name,
            str(m["documents"]),
            pct(m["all_fields"]["exact_match"]),
            pct(m["all_fields"]["normalized_match"]),
            num(m["all_fields"]["precision"]),
            num(m["all_fields"]["recall"]),
            num(m["all_fields"]["f1"]),
            pct(m["required_fields_correct_rate"]),
            pct(m["documents_fully_correct_rate"]),
        ]
        for name, m in metrics["by_method"].items()
    ]
    field_names = sorted({f for m in metrics["by_method"].values() for f in m["fields"]})
    field_header = ["Field", *[f"{name} (norm. / F1)" for name in metrics["by_method"]]]
    field_rows = [
        [
            field,
            *[
                f"{pct(m['fields'][field]['normalized_match'])} / {num(m['fields'][field]['f1'])}"
                if field in m["fields"]
                else "n/a"
                for m in metrics["by_method"].values()
            ],
        ]
        for field in field_names
    ]
    line_header = ["Input", "Row P", "Row R", "Row F1", "Cell accuracy", *LINE_COLUMNS]
    line_rows = [
        [
            name,
            num(m["line_items"]["row_precision"]),
            num(m["line_items"]["row_recall"]),
            num(m["line_items"]["row_f1"]),
            pct(m["line_items"]["cell_accuracy"]),
            *[pct(m["line_items"]["cell_accuracy_by_column"].get(c)) for c in LINE_COLUMNS],
        ]
        for name, m in metrics["by_method"].items()
    ]
    routing_header = [
        "Input",
        "Auto-accepted",
        "Error in auto bucket",
        "Analyst review",
        "Mandatory review",
        "High-confidence fields",
        "Error in high-confidence fields",
    ]
    routing_rows = [
        [
            name,
            pct(m["routing"]["auto_share"]),
            pct(m["routing"]["error_rate_in_auto"]),
            str(m["routing"]["levels"]["ANALYST_REVIEW"]),
            str(m["routing"]["levels"]["MANDATORY_REVIEW"]),
            str(m["routing"]["high_confidence_fields"]),
            pct(m["routing"]["high_confidence_field_error_rate"]),
        ]
        for name, m in metrics["by_method"].items()
    ]
    type_rows = [
        [
            name,
            str(m["documents"]),
            pct(m["all_fields"]["normalized_match"]),
            num(m["all_fields"]["f1"]),
            num(m["line_items"]["row_f1"]),
            pct(m["line_items"]["cell_accuracy"]),
            pct(m["routing"]["auto_share"]),
            pct(m["routing"]["error_rate_in_auto"]),
        ]
        for name, m in metrics["by_method_and_type"].items()
    ]
    check_rows = [
        [name, str(v["documents"]), pct(v["rate"])]
        for name, v in metrics["consistency_checks"].items()
    ]
    variant_rows = [
        [name, str(v["documents"]), pct(v["correct"])]
        for name, v in metrics["normalization_variants"].items()
    ]
    report = Report(
        quick=quick,
        suite="extraction",
        title="Structured extraction (layout extractor, no LLM)",
        dataset={
            "name": "synthetic-core",
            "seed": DATASET_SEED,
            "documents": len(manifest["documents"]),
            "held_out_seed": HELD_OUT_SEED,
            "held_out_documents": len(held_out_manifest["documents"]),
            "bundles_per_scenario": manifest["bundles_per_scenario"],
            "scanned_rerender": "light scan profile at 150 DPI",
            "vendor_master": "demo vendors: canonical names and tax IDs, no name variants",
        },
        config={
            "engine": version,
            "languages": languages,
            "quick": quick,
            "llm": "not used (EXTRACTION_LLM_MODE=never)",
            "thresholds": {
                "high": ExtractionPolicy().thresholds.high,
                "medium": ExtractionPolicy().thresholds.medium,
            },
        },
        metrics=metrics,
        environment=environment({"tesseract": version}),
        notes=[
            "Synthetic documents from one generator with three templates. The layout "
            "extractor's label vocabulary was written with these templates visible, so these "
            "numbers measure the pipeline, not generalization to unseen layouts.",
            "Exact = the extracted text equals the printed text (whitespace collapsed); "
            "normalized = the typed value equals the truth (Decimal amounts, ISO dates, vendor "
            "resolved to the canonical vendor). Precision/recall count a wrong value as both "
            "a false positive and a false negative; nulls are explicit.",
            "Fully correct = every header field and every line-item cell right. 'Error in auto "
            "bucket' is the share of auto-accepted documents with at least one error: the "
            "number that matters operationally.",
            "LLM extraction: Not yet measured (no GEMINI_API_KEY or local model in the build "
            "environment). Its merge, verification and gating logic is covered by tests.",
            "Document type taken from ground truth (classification is measured separately).",
            "Calibration: every table except the calibration ones uses the development dataset "
            f"(seed {DATASET_SEED}). The auto-accept threshold is chosen on it by a rule fixed in "
            "advance - the lowest threshold above the design floor "
            f"({DESIGN_FLOOR}: a value only a model read never auto-accepts) at which neither "
            "it nor any higher threshold auto-accepts a document with an error - and reported "
            f"unchanged on the held-out dataset (seed {HELD_OUT_SEED}). The bound is a one-sided "
            "95% Clopper-Pearson upper bound on the error rate among auto-accepted documents.",
            "ECE is the expected calibration error over ten equal-width bins; 'over-confident' "
            "counts the values in bins whose accuracy is below their mean confidence.",
        ],
        tables=[
            ("Summary by input", summary_header, summary_rows),
            (
                "Calibration: confidence against accuracy",
                [
                    "Dataset / input",
                    "Docs",
                    "Fields",
                    "Field ECE",
                    "Fields over-confident",
                    "Cells",
                    "Cell ECE",
                    "Cells over-confident",
                ],
                [
                    [
                        f"{split} / {name}",
                        str(values["documents"]),
                        str(values["fields"]["n"]),
                        num(values["fields"]["ece"]),
                        pct(values["fields"]["overconfident_share"]),
                        str(values["cells"]["n"]),
                        num(values["cells"]["ece"]),
                        pct(values["cells"]["overconfident_share"]),
                    ]
                    for split, data in (("development", development), ("held-out", held))
                    for name, values in data.items()
                ],
            ),
            (
                "Reliability (development, all inputs)",
                [
                    "Confidence",
                    "Fields",
                    "Mean confidence",
                    "Accuracy",
                    "Cells",
                    "Mean confidence",
                    "Accuracy",
                ],
                _reliability_rows(
                    development["all"]["fields"]["bins"], development["all"]["cells"]["bins"]
                ),
            ),
            (
                "Auto-accept threshold: development (choice) and held-out, all inputs",
                [
                    "Threshold",
                    "Development auto-accepted",
                    "Development errors (95% bound)",
                    "Held-out auto-accepted",
                    "Held-out errors (95% bound)",
                ],
                _sweep_rows(
                    development["all"]["sweep"],
                    held["all"]["sweep"],
                    current=current,
                    chosen=chosen,
                ),
            ),
            (
                "Auto-accept threshold: scanned inputs only",
                [
                    "Threshold",
                    "Development auto-accepted",
                    "Development errors (95% bound)",
                    "Held-out auto-accepted",
                    "Held-out errors (95% bound)",
                ],
                _sweep_rows(
                    development["scanned"]["sweep"],
                    held["scanned"]["sweep"],
                    current=current,
                    chosen=chosen,
                ),
            ),
            ("Fields: normalized match / F1", field_header, field_rows),
            ("Line items", line_header, line_rows),
            ("Confidence routing", routing_header, routing_rows),
            (
                "By input and document type",
                [
                    "Input / type",
                    "Docs",
                    "Field normalized",
                    "Field F1",
                    "Row F1",
                    "Cell accuracy",
                    "Auto-accepted",
                    "Error in auto",
                ],
                type_rows,
            ),
            (
                "Consistency checks: documents flagged inconsistent",
                ["Printed document", "Docs", "Flagged"],
                check_rows,
            ),
            (
                "Normalization of printed variants",
                ["Input / variation", "Docs", "Correct"],
                variant_rows,
            ),
        ],
    )
    report.write(output)
    return report

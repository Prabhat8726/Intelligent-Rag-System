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
* routing: share auto-accepted and the error rate inside that bucket.
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


async def run_extraction_suite(
    output: Path, *, quick: bool = False, languages: str = "eng"
) -> Report:
    ocr = TesseractOCRProvider(languages=languages, timeout_seconds=180)
    version = await ocr.verify()
    options = ExtractionOptions()
    service = FieldExtractionService(
        policy=ExtractionPolicy(llm_mode="never"),
        vendors=StaticVendorDirectory(demo_vendor_records()),
    )
    with tempfile.TemporaryDirectory(prefix="docintel-extraction-eval-") as tmp:
        root = Path(tmp)
        scenarios = (
            [Scenario.CLEAN_MATCH, Scenario.TOTAL_ARITHMETIC_ERROR, Scenario.VENDOR_NAME_VARIANT]
            if quick
            else None
        )
        manifest = generate_dataset(
            root / "dataset",
            seed=DATASET_SEED,
            bundles_per_scenario=1 if quick else 2,
            scenarios=scenarios,
        )
        jobs: list[tuple[str, Path, FileKind, dict[str, Any]]] = []
        for entry in manifest["documents"]:
            path = root / "dataset" / entry["file"]
            truth = json.loads((root / "dataset" / entry["ground_truth"]).read_text())
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

    metrics = {
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
        suite="extraction",
        title="Structured extraction (layout extractor, no LLM)",
        dataset={
            "name": "synthetic-core",
            "seed": DATASET_SEED,
            "documents": len(manifest["documents"]),
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
        ],
        tables=[
            ("Summary by input", summary_header, summary_rows),
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

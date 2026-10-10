"""Discrepancy suite: rules and duplicate detection against generator ground truth (Modules 9,
10, 25, 26, 29).

Every document of synthetic scenario bundles (purchase order, delivery note, invoice, a resent
invoice) is extracted exactly as the worker does, then matched exactly as the matching service
does (`matching.service.assess`: order lookup by reference, comparison, duplicate search, the
19 default rules). All bundles of one input form a single department, so an invoice has to
find its own order among all of them and a duplicate alarm can come from any other bundle.

Inputs: the dataset as generated (native PDFs, plus the scans of the SCANNED_DOCUMENTS bundles)
and the same documents with every native PDF re-rendered as a light scan.

Measured, with FAIL alone and with FAIL or WARN (both send a document to review):
* per planted defect: the share of documents whose defect raised its rule (recall);
* per rule: the share of its alarms that point at a planted defect (precision);
* micro precision / recall / F1 over (document, rule) pairs;
* routing: the share of defect-free documents sent to review by a rule (false alarms), and of
  documents with a defect; the extraction's own review routing is reported next to it;
* duplicates: resent invoices found (strong match), alarms on other documents.
The LLM extractor is not used (no model in the build environment): layout extraction only.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from docintel.db.models import DocumentType
from docintel.documents.validation import FileKind
from docintel.evaluation.common import DEFAULT_PARALLELISM, extract_file, run_bounded, scan_pdf
from docintel.evaluation.extraction_suite import demo_vendor_records
from docintel.evaluation.metrics import counts_prf
from docintel.evaluation.report import Report, environment, pct
from docintel.fields.confidence import ReviewLevel
from docintel.fields.service import ExtractionPolicy, ExtractionRequest, FieldExtractionService
from docintel.fields.vendors import StaticVendorDirectory
from docintel.matching.facts import DocumentFacts, facts_from_fields
from docintel.matching.service import Assessment, assess
from docintel.processing.extraction import ExtractionOptions
from docintel.processing.ocr import TesseractOCRProvider
from docintel.processing.tables import stitch_tables
from docintel.rules.defaults import DEFAULT_RULES, tolerances_from_rules
from docintel.rules.engine import Outcome
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.scenarios import Scenario

DATASET_SEED = 53
REFERENCE_DATE = date(2026, 10, 1)
MIN_CONFIDENCE = 0.85  # COMPARISON_MIN_CONFIDENCE default
MAX_LISTED_ALARMS = 30
_KINDS = {"pdf": FileKind.PDF, "png": FileKind.PNG, "tiff": FileKind.TIFF}
QUICK_SCENARIOS = [
    Scenario.CLEAN_MATCH,
    Scenario.UNIT_PRICE_MISMATCH,
    Scenario.SHORT_DELIVERY,
    Scenario.DUPLICATE_INVOICE,
]

# Planted defect -> the rules that must raise it. A quantity billed above the order is also
# above the delivery (the delivery note matches the order), so both quantity rules apply.
EXPECTED_RULES: dict[str, frozenset[str]] = {
    "UNIT_PRICE_MISMATCH": frozenset({"INV_PO_UNIT_PRICE"}),
    "QUANTITY_MISMATCH": frozenset({"INV_PO_QUANTITY", "INV_DELIVERED_QUANTITY"}),
    "BILLED_QUANTITY_EXCEEDS_DELIVERED": frozenset({"INV_DELIVERED_QUANTITY"}),
    "MISSING_PO_REFERENCE": frozenset({"INV_MISSING_PO"}),
    "TOTAL_MISMATCH": frozenset({"DOC_ARITHMETIC"}),
    "TAX_RATE_MISMATCH": frozenset({"INV_PO_TAX_RATE"}),
    "VENDOR_MISMATCH": frozenset({"INV_PO_VENDOR"}),
    "DUPLICATE_INVOICE": frozenset({"INV_DUPLICATE"}),
}
FAIL_ONLY = frozenset({Outcome.FAIL})
FAIL_OR_WARN = frozenset({Outcome.FAIL, Outcome.WARN})
LEVELS = {"fail": FAIL_ONLY, "fail_or_warn": FAIL_OR_WARN}
PO, DN = DocumentType.PURCHASE_ORDER, DocumentType.DELIVERY_NOTE


@dataclass(slots=True)
class EvaluatedDocument:
    truth: dict[str, Any]
    facts: DocumentFacts
    extraction_review: bool
    assessment: Assessment | None = None

    @property
    def doc_id(self) -> str:
        return str(self.truth["doc_id"])

    @property
    def defects(self) -> list[str]:
        return [defect["code"] for defect in self.truth["defects"]]

    @property
    def expected(self) -> frozenset[str]:
        rules: set[str] = set()
        for code in self.defects:
            rules |= EXPECTED_RULES.get(code, frozenset())
        return frozenset(rules)

    def flagged(self, outcomes: frozenset[Outcome]) -> dict[str, Any]:
        """Rule code -> result, for results with one of `outcomes`."""
        results = self.assessment.results if self.assessment else []
        return {r.rule.code: r for r in results if r.outcome in outcomes}


def match_corpus(documents: list[EvaluatedDocument]) -> None:
    """Run matching for every document with all the others on file (one department)."""
    tolerances = tolerances_from_rules(DEFAULT_RULES, min_confidence=MIN_CONFIDENCE)
    by_created = sorted(documents, key=lambda item: item.facts.created_at or datetime.min)
    for document in documents:
        facts = document.facts
        others = [item.facts for item in by_created if item is not document]
        key = facts.po_key
        orders, deliveries = [], []
        if key and facts.document_type in (DocumentType.INVOICE, DocumentType.DELIVERY_NOTE):
            orders = [o for o in others if o.document_type == PO and o.po_key == key]
        if key and facts.document_type == DocumentType.INVOICE:
            deliveries = [o for o in others if o.document_type == DN and o.po_key == key]
        document.assessment = assess(
            facts,
            orders=orders,
            deliveries=deliveries,
            # The service pre-selects by number or amount in SQL; find_duplicates decides.
            candidates=[
                other
                for other in others
                if other.document_type == facts.document_type
                and (
                    (facts.number_key is not None and other.number_key == facts.number_key)
                    or (facts.total is not None and other.total == facts.total)
                )
            ],
            rules=DEFAULT_RULES,
            tolerances=tolerances,
            reference_date=REFERENCE_DATE,
        )


def _share(values: list[bool]) -> dict[str, Any]:
    return {
        "documents": len(values),
        "rate": round(sum(values) / len(values), 4) if values else None,
    }


def score_corpus(documents: list[EvaluatedDocument]) -> dict[str, Any]:
    """Detection, precision, routing and duplicate metrics for one matched corpus."""
    metrics: dict[str, Any] = {"documents": len(documents)}
    for level, outcomes in LEVELS.items():
        by_defect: dict[str, list[bool]] = defaultdict(list)
        per_rule: dict[str, dict[str, int]] = defaultdict(lambda: {"flagged": 0, "correct": 0})
        tp = fp = fn = 0
        clean_flagged: list[bool] = []
        defective_flagged: list[bool] = []
        alarms: list[dict[str, Any]] = []
        for document in documents:
            flagged = document.flagged(outcomes)
            expected = document.expected
            for code in document.defects:
                rules = EXPECTED_RULES.get(code)
                if rules is not None:
                    by_defect[code].append(bool(rules & flagged.keys()))
            for code, result in flagged.items():
                per_rule[code]["flagged"] += 1
                if code in expected:
                    per_rule[code]["correct"] += 1
                else:
                    alarms.append(
                        {
                            "document": document.doc_id,
                            "scenario": document.truth["scenario"],
                            "rule": code,
                            "outcome": result.outcome.value,
                            "message": result.message,
                        }
                    )
            tp += len(expected & flagged.keys())
            fp += len(flagged.keys() - expected)
            fn += len(expected - flagged.keys())
            (defective_flagged if expected else clean_flagged).append(bool(flagged))
        metrics[level] = {
            "by_defect": {
                code: {
                    "documents": len(hits),
                    "detected": sum(hits),
                    "recall": round(sum(hits) / len(hits), 4),
                }
                for code, hits in sorted(by_defect.items())
            },
            "by_rule": {
                code: {
                    **counts,
                    "precision": round(counts["correct"] / counts["flagged"], 4),
                }
                for code, counts in sorted(per_rule.items())
            },
            "pairs": counts_prf(tp, fp, fn),
            "routing": {
                "defect_free_documents_flagged": _share(clean_flagged),
                "defective_documents_flagged": _share(defective_flagged),
            },
            "unexpected_alarms": alarms,
        }
    metrics["extraction_review"] = {
        "defect_free_documents": _share([d.extraction_review for d in documents if not d.expected]),
        "defective_documents": _share([d.extraction_review for d in documents if d.expected]),
    }
    metrics["duplicates"] = score_duplicates(documents)
    metrics["rule_errors"] = [
        {"document": d.doc_id, "rule": r.rule.code, "message": r.message}
        for d in documents
        for r in (d.assessment.results if d.assessment else [])
        if r.outcome == Outcome.ERROR
    ]
    return metrics


def score_duplicates(documents: list[EvaluatedDocument]) -> dict[str, Any]:
    """Resent invoices (truth: DUPLICATE_INVOICE -> original) against the matches found."""
    truth = {
        (document.doc_id, defect["duplicate_of"])
        for document in documents
        for defect in document.truth["defects"]
        if defect["code"] == "DUPLICATE_INVOICE"
    }
    found: dict[str, set[tuple[str, str]]] = {"strong": set(), "possible": set()}
    for document in documents:
        for match in document.assessment.duplicates if document.assessment else []:
            # Labels are the ground-truth document ids (see `extract`).
            found["strong" if match.strong else "possible"].add((document.doc_id, match.label))
    strong = found["strong"]
    either = strong | found["possible"]
    return {
        "resent_invoices": len(truth),
        "strong": counts_prf(len(strong & truth), len(strong - truth), len(truth - strong)),
        "strong_or_possible": counts_prf(
            len(either & truth), len(either - truth), len(truth - either)
        ),
        "wrong_pairs": sorted(f"{doc} -> {original}" for doc, original in either - truth),
    }


async def run_discrepancy_suite(
    output: Path, *, quick: bool = False, languages: str = "eng"
) -> Report:
    ocr = TesseractOCRProvider(languages=languages, timeout_seconds=180)
    version = await ocr.verify()
    options = ExtractionOptions()
    service = FieldExtractionService(
        policy=ExtractionPolicy(llm_mode="never"),
        vendors=StaticVendorDirectory(demo_vendor_records()),
    )
    bundles = 1 if quick else 4
    with tempfile.TemporaryDirectory(prefix="docintel-discrepancy-eval-") as tmp:
        root = Path(tmp)
        manifest = generate_dataset(
            root / "dataset",
            seed=DATASET_SEED,
            bundles_per_scenario=bundles,
            scenarios=QUICK_SCENARIOS if quick else None,
        )
        inputs: dict[str, list[tuple[int, Path, FileKind, dict[str, Any]]]] = defaultdict(list)
        for position, entry in enumerate(manifest["documents"]):
            path = root / "dataset" / entry["file"]
            truth = json.loads((root / "dataset" / entry["ground_truth"]).read_text())
            kind = _KINDS[path.suffix.lstrip(".")]
            inputs["as generated"].append((position, path, kind, truth))
            if entry["variant"] == "native":
                scanned = path.with_name(path.stem + "-scan.pdf")
                scanned.write_bytes(scan_pdf(path.read_bytes(), seed=position))
                inputs["all scanned"].append((position, scanned, FileKind.PDF, truth))
            else:
                inputs["all scanned"].append((position, path, kind, truth))

        async def extract(job: tuple[int, Path, FileKind, dict[str, Any]]) -> EvaluatedDocument:
            position, path, kind, truth = job
            doc_type = DocumentType(truth["document_type"])
            pages = await extract_file(path, kind, ocr, options)
            outcome = await service.extract(
                ExtractionRequest(doc_type, pages, stitch_tables(pages))
            )
            if outcome is None:  # pragma: no cover - every bundle type has a schema
                msg = f"no extraction schema for {doc_type}"
                raise RuntimeError(msg)
            facts = facts_from_fields(
                doc_type,
                outcome.fields,
                checks=[check.to_json() for check in outcome.scoring.checks],
                document_id=uuid.uuid5(uuid.NAMESPACE_URL, f"docintel-eval:{truth['doc_id']}"),
                label=truth["doc_id"],
                # Upload order = generation order: an original is always older than its copy.
                created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=position),
            )
            return EvaluatedDocument(truth, facts, outcome.scoring.level != ReviewLevel.AUTO)

        corpora: dict[str, list[EvaluatedDocument]] = {}
        for name, jobs in inputs.items():
            corpora[name] = await run_bounded(jobs, extract, DEFAULT_PARALLELISM)
            match_corpus(corpora[name])

    metrics = {name: score_corpus(documents) for name, documents in corpora.items()}
    report = Report(
        quick=quick,
        suite="discrepancies",
        title="Discrepancy and duplicate detection",
        dataset={
            "name": manifest["dataset"],
            "generator_version": manifest["generator_version"],
            "seed": DATASET_SEED,
            "bundles_per_scenario": bundles,
            "scenarios": sorted({entry["scenario"] for entry in manifest["documents"]}),
            "documents_per_input": len(manifest["documents"]),
            "quick": quick,
        },
        config={
            "rules": "default rule set (19 rules, default parameters)",
            "comparison_min_confidence": MIN_CONFIDENCE,
            "reference_date": REFERENCE_DATE.isoformat(),
            "llm_mode": "never",
            "expected_rules": {code: sorted(rules) for code, rules in EXPECTED_RULES.items()},
            "ocr_languages": languages,
        },
        metrics=metrics,
        environment=environment({"tesseract": version}),
    )
    _tables(report, metrics)
    report.write(output)
    return report


def _cell(stats: dict[str, Any]) -> str:
    return f"{stats['detected']}/{stats['documents']} ({pct(stats['recall'])})"


def _tables(report: Report, metrics: dict[str, Any]) -> None:
    overall_rows = []
    for name, corpus in metrics.items():
        fail, either = corpus["fail"], corpus["fail_or_warn"]
        overall_rows.append(
            [
                name,
                str(corpus["documents"]),
                f"{pct(fail['pairs']['precision'])} / {pct(fail['pairs']['recall'])}",
                f"{pct(either['pairs']['precision'])} / {pct(either['pairs']['recall'])}",
                pct(fail["routing"]["defect_free_documents_flagged"]["rate"]),
                pct(either["routing"]["defect_free_documents_flagged"]["rate"]),
                pct(either["routing"]["defective_documents_flagged"]["rate"]),
                pct(corpus["extraction_review"]["defect_free_documents"]["rate"]),
            ]
        )
    report.tables.append(
        (
            "Overall",
            [
                "Input",
                "Docs",
                "P / R (fail)",
                "P / R (fail or warn)",
                "Defect-free flagged (fail)",
                "Defect-free flagged (fail or warn)",
                "Defective flagged",
                "Defect-free in extraction review",
            ],
            overall_rows,
        )
    )
    defect_rows = []
    for name, corpus in metrics.items():
        for code, stats in corpus["fail"]["by_defect"].items():
            either = corpus["fail_or_warn"]["by_defect"][code]
            defect_rows.append(
                [name, code, ", ".join(sorted(EXPECTED_RULES[code])), _cell(stats), _cell(either)]
            )
    report.tables.append(
        (
            "Detection by planted defect",
            ["Input", "Defect", "Rule(s)", "Detected (fail)", "Detected (fail or warn)"],
            defect_rows,
        )
    )
    rule_rows = []
    for name, corpus in metrics.items():
        codes = sorted(set(corpus["fail"]["by_rule"]) | set(corpus["fail_or_warn"]["by_rule"]))
        for code in codes:
            fail = corpus["fail"]["by_rule"].get(code, {"flagged": 0, "correct": 0})
            either = corpus["fail_or_warn"]["by_rule"][code]
            rule_rows.append(
                [
                    name,
                    code,
                    f"{fail['correct']}/{fail['flagged']}",
                    f"{either['correct']}/{either['flagged']}",
                    pct(either["precision"]),
                ]
            )
    report.tables.append(
        (
            "Alarms per rule (correct / raised)",
            ["Input", "Rule", "Fail", "Fail or warn", "Precision (fail or warn)"],
            rule_rows,
        )
    )
    duplicate_rows = [
        [
            name,
            str(corpus["duplicates"]["resent_invoices"]),
            f"{pct(corpus['duplicates']['strong']['precision'])} / "
            f"{pct(corpus['duplicates']['strong']['recall'])}",
            f"{pct(corpus['duplicates']['strong_or_possible']['precision'])} / "
            f"{pct(corpus['duplicates']['strong_or_possible']['recall'])}",
            ", ".join(corpus["duplicates"]["wrong_pairs"]) or "none",
        ]
        for name, corpus in metrics.items()
    ]
    report.tables.append(
        (
            "Duplicate invoices",
            ["Input", "Resent invoices", "P / R (strong)", "P / R (strong or possible)", "Wrong"],
            duplicate_rows,
        )
    )
    alarm_rows = [
        [name, alarm["document"], alarm["rule"], alarm["outcome"], alarm["message"]]
        for name, corpus in metrics.items()
        for alarm in corpus["fail_or_warn"]["unexpected_alarms"]
    ]
    report.tables.append(
        (
            "Alarms without a planted defect",
            ["Input", "Document", "Rule", "Outcome", "Message"],
            [[cell.replace("|", "\\|") for cell in row] for row in alarm_rows[:MAX_LISTED_ALARMS]],
        )
    )
    report.notes += [
        "Every bundle of an input is matched in one department: the invoice must find its "
        "own order and delivery note by reference, and duplicate alarms can come from any "
        "bundle. Documents are matched with all others on file (the state after the last "
        "upload, whatever the arrival order).",
        "FAIL is a confirmed discrepancy; WARN means a difference involves a value read with "
        "low confidence (or the referenced order is not on file). Both send the document to "
        "the review queue; the columns show which of the two raised the alarm.",
        "A defect counts as detected when one of its rules raised it. Pair precision / recall "
        "count (document, rule) pairs, so a quantity billed above the order needs both "
        "quantity rules.",
        "'Defect-free in extraction review' is the extraction's own routing (uncertain or "
        "missing values), independent of the rules; the review queue takes both.",
        f"Alarms without a planted defect: {len(alarm_rows)} in total, the first "
        f"{min(len(alarm_rows), MAX_LISTED_ALARMS)} listed; the JSON report has all of them.",
        "Byte-identical re-uploads are caught by the file hash at upload (exact, not measured "
        "here); this suite measures resent invoices with a different layout and date format.",
    ]

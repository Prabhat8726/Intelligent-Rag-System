"""Evaluation metrics and report writing (values computed by hand)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from docintel.evaluation.calibration import (
    DocumentOutcome,
    calibration_error,
    choose_threshold,
    error_upper_bound,
    overconfident_share,
    reliability,
    threshold_sweep,
)
from docintel.evaluation.discrepancy_suite import EvaluatedDocument, match_corpus, score_corpus
from docintel.evaluation.metrics import (
    Prediction,
    bag_of_words_f1,
    cer,
    classification_report,
    counts_prf,
    expected_calibration_error,
    summary,
    wer,
)
from docintel.evaluation.report import Report
from docintel.evaluation.system_suite import PipelineRun, percentile, spread, summarize
from docintel.evaluation.tables_suite import score_table
from docintel.evaluation.versions_suite import score_segmentation, score_step
from docintel.evaluation.workflow_suite import (
    contract_deviations,
    expected_contract_rules,
    score_changes,
)
from docintel.matching.facts import DocumentFacts
from docintel.processing.content import BBox, DocumentTable, TableRow
from docintel.versions.clauses import segment
from tests.factories.facts import delivery, invoice, line, purchase_order


def test_character_and_word_error_rates() -> None:
    assert cer("invoice", "invoice") == 0.0
    assert cer("invoice", "invoicc") == pytest.approx(1 / 7)
    assert cer("", "") == 0.0
    assert cer("", "x") == 1.0
    assert wer("total due 71.55", "total dua 71.55") == pytest.approx(1 / 3)
    assert wer("a b c", "a c") == pytest.approx(1 / 3)
    assert wer("a b", "a b x y") == 1.0  # insertions count too


def test_bag_of_words_f1_ignores_order() -> None:
    assert bag_of_words_f1("a b c", "c b a") == 1.0
    assert bag_of_words_f1("a b c d", "a b") == pytest.approx(2 * 1 * 0.5 / 1.5)
    assert bag_of_words_f1("", "") == 1.0
    assert bag_of_words_f1("a", "") == 0.0


def test_summary() -> None:
    assert summary([1.0, 2.0, 4.0]) == {
        "mean": 2.3333,
        "median": 2.0,
        "min": 1.0,
        "max": 4.0,
        "n": 3,
    }
    assert summary([])["n"] == 0


def test_classification_report() -> None:
    predictions = [
        Prediction("INVOICE", "INVOICE", 0.95),
        Prediction("INVOICE", "RECEIPT", 0.55),
        Prediction("RECEIPT", "RECEIPT", 0.90),
        Prediction("RECEIPT", "RECEIPT", 0.65),
    ]
    report = classification_report(predictions, ["INVOICE", "RECEIPT", "OTHER"], threshold=0.7)
    assert report["accuracy"] == 0.75
    per_class = report["per_class"]
    assert isinstance(per_class, dict)
    assert per_class["INVOICE"] == {"precision": 1.0, "recall": 0.5, "f1": 0.6667, "support": 2}
    assert per_class["RECEIPT"] == {"precision": 0.6667, "recall": 1.0, "f1": 0.8, "support": 2}
    assert report["macro_f1"] == pytest.approx((0.6667 + 0.8) / 2, abs=1e-4)  # OTHER has no support
    confusion = report["confusion_matrix"]
    assert isinstance(confusion, dict)
    assert confusion["INVOICE"] == {"INVOICE": 1, "RECEIPT": 1, "OTHER": 0}
    assert report["routing"] == {
        "threshold": 0.7,
        "auto_accepted_share": 0.5,
        "error_rate_in_auto_bucket": 0.0,
        "review_share": 0.5,
    }


def test_expected_calibration_error() -> None:
    perfect = [Prediction("A", "A", 1.0), Prediction("A", "B", 0.0)]
    assert expected_calibration_error(perfect) == 0.0
    overconfident = [Prediction("A", "B", 0.95), Prediction("A", "A", 0.95)]
    assert expected_calibration_error(overconfident) == pytest.approx(0.45)


def _table(rows: list[list[str]]) -> DocumentTable:
    box = BBox(0, 0, 1, 1)
    return DocumentTable(
        page_start=1,
        page_end=1,
        bbox=box,
        header=["#", "Item", "Description", "Qty", "Unit"],
        rows=[TableRow(cells, box, 1) for cells in rows],
        method="NATIVE",
        confidence=1.0,
    )


def test_table_scoring_matches_rows_by_sku() -> None:
    expected = [["1", "SKU-1", "Bolt", "5", "pcs"], ["2", "SKU-2", "Nut", "10", "pcs"]]
    detected = _table([["1", "SKU-1", "Bolt", "5", "pcs"], ["2", "SKU-2", "Nut", "1O", "pcs"]])
    score = score_table(detected, expected)
    assert (score["matched"], score["detected_rows"], score["expected_rows"]) == (2, 2, 2)
    assert score["cells"]["quantity"] == [True, False]
    misread = score_table(_table([["1", "SKV-1", "Bolt", "5", "pcs"]]), expected)
    assert misread["matched"] == 1  # one misread character is a cell error, not a lost row
    assert misread["cells"]["sku"] == [False]
    unrelated = score_table(_table([["1", "XYZ-9", "Bolt", "5", "pcs"]]), expected)
    assert unrelated["matched"] == 0
    assert score_table(None, expected)["found"] is False


def test_report_files(tmp_path: Path) -> None:
    report = Report(
        suite="demo",
        title="Demo report",
        dataset={"seed": 1},
        config={"threshold": 0.7},
        metrics={"accuracy": 0.9},
        environment={"python": "3.13"},
        notes=["synthetic"],
        tables=[("Summary", ["Set", "Accuracy"], [["text", "90.0%"]])],
    )
    json_path, markdown_path = report.write(tmp_path)
    data = json.loads(json_path.read_text())
    assert data["metrics"] == {"accuracy": 0.9}
    assert data["created_at"]
    markdown = markdown_path.read_text()
    assert "| text | 90.0% |" in markdown
    assert "docintel evaluate --suite demo" in markdown
    assert "* synthetic" in markdown


async def test_extraction_scoring_on_a_native_synthetic_invoice(tmp_path: Path) -> None:
    from docintel.db.models import DocumentType
    from docintel.documents.validation import FileKind
    from docintel.evaluation.common import extract_file
    from docintel.evaluation.extraction_suite import (
        aggregate,
        demo_vendor_records,
        score_document,
    )
    from docintel.fields.service import (
        ExtractionPolicy,
        ExtractionRequest,
        FieldExtractionService,
    )
    from docintel.fields.vendors import StaticVendorDirectory
    from docintel.processing.extraction import ExtractionOptions
    from docintel.processing.tables import stitch_tables
    from docintel.synthetic.generator import generate_dataset
    from docintel.synthetic.scenarios import Scenario
    from tests.unit.test_extraction import FakeOCR

    manifest = generate_dataset(tmp_path, seed=8, scenarios=[Scenario.VENDOR_NAME_VARIANT])
    entry = next(d for d in manifest["documents"] if d["document_type"] == "INVOICE")
    truth = json.loads((tmp_path / entry["ground_truth"]).read_text())
    pages = await extract_file(
        tmp_path / entry["file"], FileKind.PDF, FakeOCR(), ExtractionOptions()
    )
    service = FieldExtractionService(
        policy=ExtractionPolicy(llm_mode="never"),
        vendors=StaticVendorDirectory(demo_vendor_records()),
    )
    outcome = await service.extract(
        ExtractionRequest(DocumentType.INVOICE, pages, stitch_tables(pages))
    )
    assert outcome is not None
    score = score_document(DocumentType.INVOICE, truth, outcome)
    # A printed vendor-name variant and another date format: both normalized correctly.
    assert score["fields"]["vendor_name"]["correct"]
    assert score["fields"]["invoice_date"]["correct"]
    assert all(record["correct"] == record["truth"] for record in score["fields"].values())
    assert score["rows"]["matched"] == len(truth["line_items"])
    assert not score["any_error"]
    summary = aggregate([score])
    assert summary["all_fields"]["f1"] == 1.0
    assert summary["line_items"]["cell_accuracy"] == 1.0
    assert summary["documents_fully_correct_rate"] == 1.0


def test_counts_prf() -> None:
    assert counts_prf(3, 1, 2) == {
        "tp": 3,
        "fp": 1,
        "fn": 2,
        "precision": 0.75,
        "recall": 0.6,
        "f1": 0.6667,
    }
    assert counts_prf(0, 0, 0)["precision"] is None


def _evaluated(
    doc_id: str, facts: DocumentFacts, defects: list[dict[str, str]]
) -> EvaluatedDocument:
    truth = {"doc_id": doc_id, "scenario": "TEST", "defects": defects}
    return EvaluatedDocument(truth, facts, extraction_review=False)


def test_discrepancy_scoring_counts_planted_defects_and_false_alarms() -> None:
    ordered = [line(0, "BRG-6204", 10, "4.85"), line(1, "VLV-BL050", 2, "38.40")]
    billed = [line(0, "BRG-6204", 10, "5.25"), line(1, "VLV-BL050", 2, "38.40")]
    corpus = [
        _evaluated("B1-PO", purchase_order(ordered, number="PO-2026-10001", total="100"), []),
        _evaluated("B1-DN", delivery(ordered, number="DN-1", po="PO-2026-10001"), []),
        _evaluated(
            "B1-INV",
            invoice(billed, number="INV-1", total="100", created_minutes=1),
            [{"code": "UNIT_PRICE_MISMATCH"}],
        ),
        _evaluated(
            "B1-INV2",
            invoice(billed, number="INV-1", total="100", created_minutes=2),
            [
                {"code": "UNIT_PRICE_MISMATCH"},
                {"code": "DUPLICATE_INVOICE", "duplicate_of": "B1-INV"},
            ],
        ),
        # Clean, but its order is not on file: a false alarm (WARN).
        _evaluated("B2-INV", invoice(ordered, number="INV-9", po="PO-2026-99999", total="9"), []),
    ]
    for document in corpus:
        document.facts.label = document.doc_id
    match_corpus(corpus)
    metrics = score_corpus(corpus)

    fail = metrics["fail"]
    assert fail["by_defect"]["UNIT_PRICE_MISMATCH"] == {
        "documents": 2,
        "detected": 2,
        "recall": 1.0,
    }
    assert fail["by_defect"]["DUPLICATE_INVOICE"]["recall"] == 1.0
    assert fail["unexpected_alarms"] == [], fail["unexpected_alarms"]
    assert fail["pairs"]["precision"] == 1.0
    assert fail["routing"]["defect_free_documents_flagged"] == {"documents": 3, "rate": 0.0}
    either = metrics["fail_or_warn"]
    assert either["by_rule"]["INV_MISSING_PO"] == {"flagged": 1, "correct": 0, "precision": 0.0}
    assert either["routing"]["defect_free_documents_flagged"]["rate"] == round(1 / 3, 4)
    assert [alarm["document"] for alarm in either["unexpected_alarms"]] == ["B2-INV"]
    duplicates = metrics["duplicates"]
    assert duplicates["strong"]["precision"] == duplicates["strong"]["recall"] == 1.0
    assert duplicates["wrong_pairs"] == []


CONTRACT_V1 = [
    "SUPPLY AGREEMENT\nVersion 1\n1. Term\nTwo years.\n2. Fees and Payment\n"
    "Invoices are payable within 30 days.\n3. Insurance\nCover of 1,000,000 USD.\n"
    "IN WITNESS WHEREOF the Parties have signed."
]
CONTRACT_V2 = [
    "SUPPLY AGREEMENT\nVersion 2\n1. Term\nTwo years.\n2. Data Protection\n"
    "Personal data is processed under the DPA.\n3. Fees and Payment\n"
    "Invoices are payable within 45 days.\nIN WITNESS WHEREOF the Parties have signed."
]


def test_version_scoring_by_change_type() -> None:
    truth = {
        "added": ["Data Protection"],
        "removed": ["Insurance"],
        "modified": ["Fees and Payment"],
    }
    old, new = segment(CONTRACT_V1), segment(CONTRACT_V2)
    scores = score_step(old, new, truth)
    # Renumbering ("Fees and Payment" 2 -> 3) and the preamble's version line are not changes.
    assert scores == {
        "added": {"tp": 1, "fp": 0, "fn": 0},
        "removed": {"tp": 1, "fp": 0, "fn": 0},
        "modified": {"tp": 1, "fp": 0, "fn": 0},
    }
    wrong = score_step(old, new, {"added": [], "removed": [], "modified": ["Term"]})
    assert wrong["modified"] == {"tp": 0, "fp": 1, "fn": 1}
    titles = {"clauses": [{"title": "Term"}, {"title": "Fees and Payment"}, {"title": "Insurance"}]}
    assert score_segmentation(old, titles) == {"count": True, "titles": True}


def test_contract_workflow_expectations_come_from_the_generator_record() -> None:
    version: dict[str, Any] = {
        "expiration_date": "2027-03-01",
        "guidelines": {
            "missing_required": [],
            "governing_law": "the State of Ohio",
            "termination_notice_days": 90,
        },
    }
    today = date(2026, 10, 1)
    clean = expected_contract_rules(version, today)
    assert set(clean.values()) == {"PASS"}
    assert contract_deviations(clean) == []

    version["guidelines"] = {
        "missing_required": ["Governing Law"],
        "governing_law": None,
        "termination_notice_days": 120,
    }
    version["expiration_date"] = "2026-10-20"
    deviating = expected_contract_rules(version, today)
    assert deviating == {
        "CONTRACT_REQUIRED_CLAUSES": "FAIL",
        "CONTRACT_TERMINATION_NOTICE": "FAIL",
        "CONTRACT_GOVERNING_LAW": "NOT_APPLICABLE",
        "CONTRACT_EXPIRY": "WARN",  # within 30 days
    }
    assert contract_deviations(deviating) == [
        "CONTRACT_REQUIRED_CLAUSES",
        "CONTRACT_TERMINATION_NOTICE",
        "CONTRACT_EXPIRY",
    ]
    version["guidelines"]["governing_law"] = "Germany"
    version["expiration_date"] = "2026-09-30"
    assert expected_contract_rules(version, today)["CONTRACT_GOVERNING_LAW"] == "WARN"
    assert expected_contract_rules(version, today)["CONTRACT_EXPIRY"] == "FAIL"


def test_workflow_version_changes_are_scored_by_title_and_type() -> None:
    compared = {
        "clauses": [
            {"title": "Preamble", "change": "MODIFIED"},  # not a clause
            {"title": "Fees and Paymnet", "change": "MODIFIED"},  # one character off (OCR)
            {"title": "Insurance", "change": "ADDED"},  # really removed
        ]
    }
    truth = {"added": [], "removed": ["Insurance"], "modified": ["Fees and Payment"]}
    assert score_changes(compared, truth) == {"tp": 1, "fp": 1, "fn": 1, "exact": False}
    exact = {"clauses": [{"title": "Insurance", "change": "REMOVED"}]}
    assert score_changes(exact, {"removed": ["Insurance"]})["exact"] is True
    assert score_changes(None, {"added": ["Insurance"]}) == {
        "tp": 0,
        "fp": 0,
        "fn": 1,
        "exact": False,
    }


def test_percentiles_are_nearest_rank_and_empty_is_unknown() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 0.5) == 51.0  # nearest rank, no interpolation
    assert percentile(values, 0.95) == 95.0
    assert percentile([], 0.5) is None
    assert spread([2.0, 1.0, 3.0]) == {"n": 3, "p50": 2.0, "p95": 3.0}


def test_system_summary_rates_come_from_the_wall_time() -> None:
    run = PipelineRun(
        concurrency=4,
        documents=10,
        pages=25,
        wall_seconds=30.0,
        completed=9,
        failed=1,
        retried=2,
        review_required=3,
        job_seconds={"native": [0.2, 0.4], "scanned": [2.0], "all": [0.2, 0.4, 2.0]},
        stage_ms={"extract": {"native": [50.0], "scanned": [1500.0], "all": [50.0, 1500.0]}},
        upload_ms=[30.0, 40.0],
        llm_calls=0,
    )
    summary = summarize(run)
    assert summary["documents_per_minute"] == 18.0  # completed documents only
    assert summary["pages_per_minute"] == 50.0
    assert summary["failure_rate"] == 0.1
    assert summary["processing_seconds"]["scanned"] == {"n": 1, "p50": 2.0, "p95": 2.0}
    assert summary["stage_ms"]["extract"]["native"]["p50"] == 50.0


def test_reliability_and_ece_measure_the_gap_between_confidence_and_accuracy() -> None:
    # 0.9 confident and always right: under-confident by 0.1; 0.35 and right half the time.
    pairs = [(0.9, True)] * 10 + [(0.35, True), (0.35, False)] * 5
    bins = reliability(pairs)
    assert [(b["low"], b["n"], b["accuracy"]) for b in bins] == [(0.3, 10, 0.5), (0.9, 10, 1.0)]
    assert calibration_error(pairs) == 0.125  # (0.15 + 0.1) / 2
    assert overconfident_share(pairs) == 0.0  # nobody is more confident than right
    assert overconfident_share([(0.95, False), (0.95, True)]) == 1.0
    assert reliability([(1.0, True)])[0]["high"] == 1.0  # the top bin includes 1.0
    assert calibration_error([]) is None


def test_zero_errors_is_not_a_zero_error_rate() -> None:
    assert error_upper_bound(0, 0) is None
    bound = error_upper_bound(0, 100)
    assert bound is not None
    assert 0.029 < bound < 0.031  # about 3/n
    assert error_upper_bound(5, 5) == 1.0


def test_the_threshold_rule_stays_above_the_floor_and_below_every_error() -> None:
    documents = [
        DocumentOutcome(0.97, failed_checks=False, any_error=False),
        DocumentOutcome(0.86, failed_checks=False, any_error=False),
        DocumentOutcome(0.83, failed_checks=False, any_error=True),
        DocumentOutcome(0.99, failed_checks=True, any_error=True),  # a failed check: never auto
        DocumentOutcome(0.6, failed_checks=False, any_error=False),
    ]
    sweep = threshold_sweep(documents, grid=(0.5, 0.82, 0.85, 0.9))
    assert [(row["threshold"], row["auto"], row["errors"]) for row in sweep] == [
        (0.5, 4, 1),
        (0.82, 3, 1),
        (0.85, 2, 0),
        (0.9, 1, 0),
    ]
    assert choose_threshold(sweep) == 0.85
    # Error-free all the way down: the design floor (0.8) still bounds the choice.
    clean = threshold_sweep([DocumentOutcome(0.6, failed_checks=False, any_error=False)])
    assert choose_threshold(clean) == 0.82
    # An error even at the top: no threshold of the grid is safe.
    worst = threshold_sweep([DocumentOutcome(1.0, failed_checks=False, any_error=True)])
    assert choose_threshold(worst) is None

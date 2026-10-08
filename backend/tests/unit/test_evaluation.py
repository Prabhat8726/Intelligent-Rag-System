"""Evaluation metrics and report writing (values computed by hand)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docintel.evaluation.metrics import (
    Prediction,
    bag_of_words_f1,
    cer,
    classification_report,
    expected_calibration_error,
    summary,
    wer,
)
from docintel.evaluation.report import Report
from docintel.evaluation.tables_suite import score_table
from docintel.processing.content import BBox, DocumentTable, TableRow


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

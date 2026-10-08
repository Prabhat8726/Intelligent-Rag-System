"""Synthetic generator: deterministic, internally consistent, and defects are really present."""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytest

from docintel.documents.validation import FileKind, UploadLimits, validate_file
from docintel.processing.inspection import InspectionKind, inspect_file
from docintel.synthetic.catalog import LOCALES, VENDORS
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.render import format_money
from docintel.synthetic.scenarios import Scenario

LIMITS = UploadLimits(max_bytes=25 * 1024 * 1024, max_pages=200, max_image_pixels=50_000_000)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    root = tmp_path_factory.mktemp("synthetic")
    return root, generate_dataset(root, seed=7, bundles_per_scenario=1)


def _truth(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((root / entry["ground_truth"]).read_text())
    return data


def _by_id(root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["doc_id"]: _truth(root, entry) for entry in manifest["documents"]}


def _pdf_text(path: Path) -> str:
    document = pdfium.PdfDocument(path)
    try:
        return "\n".join(document[i].get_textpage().get_text_range() for i in range(len(document)))
    finally:
        document.close()


def test_generation_is_deterministic(tmp_path: Path) -> None:
    first = generate_dataset(
        tmp_path / "a", seed=11, scenarios=[Scenario.SCANNED_DOCUMENTS, Scenario.CLEAN_MATCH]
    )
    second = generate_dataset(
        tmp_path / "b", seed=11, scenarios=[Scenario.SCANNED_DOCUMENTS, Scenario.CLEAN_MATCH]
    )
    third = generate_dataset(tmp_path / "c", seed=12, scenarios=[Scenario.CLEAN_MATCH])
    assert first == second
    assert [d["sha256"] for d in first["documents"]][:3] != [
        d["sha256"] for d in third["documents"]
    ]


def test_manifest_covers_every_scenario(dataset: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = dataset
    assert {entry["scenario"] for entry in manifest["documents"]} == {s.value for s in Scenario}
    assert manifest["document_count"] == len(manifest["documents"])
    for entry in manifest["documents"]:
        assert (root / entry["file"]).is_file()
        assert (root / entry["ground_truth"]).is_file()


def test_every_file_passes_upload_validation(dataset: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = dataset
    for entry in manifest["documents"]:
        path = root / entry["file"]
        result = validate_file(
            path, filename=path.name, content_type=entry["mime_type"], limits=LIMITS
        )
        assert result.mime_type == entry["mime_type"]
        assert result.sha256 == entry["sha256"]


def test_amounts_are_consistent_unless_a_defect_says_otherwise(
    dataset: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = dataset
    for truth in _by_id(root, manifest).values():
        fields = truth["fields"]
        if fields["total"] is None:
            continue
        lines = sum(Decimal(item["line_total"]) for item in truth["line_items"])
        for item in truth["line_items"]:
            expected = (Decimal(item["quantity"]) * Decimal(item["unit_price"])).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            assert Decimal(item["line_total"]) == expected
        assert Decimal(fields["subtotal"]) == lines
        codes = {defect["code"] for defect in truth["defects"]}
        subtotal, tax = Decimal(fields["subtotal"]), Decimal(fields["tax"])
        if "TOTAL_MISMATCH" in codes:
            assert Decimal(fields["total"]) != subtotal + tax
        else:
            assert Decimal(fields["total"]) == subtotal + tax


def test_defects_are_really_present_in_the_documents(dataset: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = dataset
    truths = _by_id(root, manifest)
    for doc_id, truth in truths.items():
        for defect in truth["defects"]:
            code = defect["code"]
            if code in {
                "UNIT_PRICE_MISMATCH",
                "QUANTITY_MISMATCH",
                "BILLED_QUANTITY_EXCEEDS_DELIVERED",
            }:
                other = truths[defect["compared_with"]]
                field = "unit_price" if code == "UNIT_PRICE_MISMATCH" else "quantity"
                mine = {i["sku"]: i for i in truth["line_items"]}
                theirs = {i["sku"]: i for i in other["line_items"]}
                differing = [sku for sku in mine if mine[sku][field] != theirs[sku][field]]
                assert differing == [defect["sku"]], doc_id
                assert mine[defect["sku"]][field] == defect["actual"]
                assert theirs[defect["sku"]][field] == defect["expected"]
            elif code == "MISSING_PO_REFERENCE":
                assert truth["fields"]["purchase_order_number"] is None
                assert "PO Reference" not in _pdf_text(root / f"{truth['bundle_id']}/{doc_id}.pdf")
            elif code == "VENDOR_MISMATCH":
                po = truths[f"{truth['bundle_id']}-PO"]
                assert truth["fields"]["vendor_code"] != po["fields"]["vendor_code"]
            elif code == "TAX_RATE_MISMATCH":
                country = next(
                    v.country for v in VENDORS if v.code == truth["fields"]["vendor_code"]
                )
                assert Decimal(truth["fields"]["tax_rate"]) != LOCALES[country].tax_rate
            elif code == "DUPLICATE_INVOICE":
                original = truths[defect["duplicate_of"]]
                assert truth["fields"]["number"] == original["fields"]["number"]
                assert truth["fields"]["total"] == original["fields"]["total"]
                assert truth["rendering"]["template"] != original["rendering"]["template"]


def test_native_pdfs_carry_the_printed_values(dataset: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = dataset
    for entry in manifest["documents"]:
        if entry["variant"] != "native" or entry["document_type"] != "INVOICE":
            continue
        truth = _truth(root, entry)
        text = _pdf_text(root / entry["file"])
        vendor = next(v for v in VENDORS if v.code == truth["fields"]["vendor_code"])
        assert truth["fields"]["number"] in text
        assert truth["fields"]["vendor_name"] in text
        assert format_money(Decimal(truth["fields"]["total"]), LOCALES[vendor.country]) in text


def test_scanned_documents_need_ocr(dataset: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = dataset
    scanned = [entry for entry in manifest["documents"] if entry["variant"] == "scanned"]
    assert scanned
    for entry in scanned:
        kind = {"application/pdf": "pdf", "image/png": "png", "image/tiff": "tiff"}[
            entry["mime_type"]
        ]
        inspection = inspect_file(root / entry["file"], FileKind(kind))
        assert inspection.kind in {InspectionKind.SCANNED_PDF, InspectionKind.IMAGE}
        assert inspection.pages_needing_ocr == list(range(1, inspection.page_count + 1))


def test_long_documents_span_pages_with_repeated_table_header(
    dataset: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = dataset
    entry = next(
        e
        for e in manifest["documents"]
        if e["scenario"] == "LONG_MULTIPAGE" and e["document_type"] == "INVOICE"
    )
    document = pdfium.PdfDocument(root / entry["file"])
    try:
        assert len(document) >= 2
        assert "Description" in document[1].get_textpage().get_text_range()
    finally:
        document.close()


def test_skus_are_unique_within_each_document(dataset: tuple[Path, dict[str, Any]]) -> None:
    root, manifest = dataset
    for truth in _by_id(root, manifest).values():
        skus = [item["sku"] for item in truth["line_items"]]
        assert len(skus) == len(set(skus)), truth["doc_id"]

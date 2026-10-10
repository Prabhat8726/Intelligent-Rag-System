"""Business rule engine (Module 10): evaluators, parameters, defaults and real documents."""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from docintel.db.models import DocumentType
from docintel.fields.schemas import ValueType
from docintel.matching.compare import Tolerances, compare_delivery, compare_invoice
from docintel.matching.duplicates import find_duplicates
from docintel.matching.facts import ClauseFact, DocumentFacts, FactValue, facts_from_fields
from docintel.rules.defaults import DEFAULT_RULES, duplicate_params, tolerances_from_rules
from docintel.rules.engine import (
    Finding,
    InvalidRuleParamsError,
    Outcome,
    RuleContext,
    RuleDefinition,
    RuleParams,
    Severity,
    evaluate,
    register,
    rule_types,
    validate_params,
)
from docintel.synthetic.contracts import contract_family
from tests.factories.facts import delivery, document, invoice, line, purchase_order

TODAY = date(2026, 3, 20)
TOL = tolerances_from_rules(DEFAULT_RULES, min_confidence=0.85)
ORDER = [line(0, "BRG-6204", 10, "4.85"), line(1, "VLV-BL050", 2, "38.40")]


def results(
    facts: DocumentFacts,
    *,
    order: DocumentFacts | None = None,
    notes: list[DocumentFacts] | None = None,
    others: list[DocumentFacts] | None = None,
    rules: tuple[RuleDefinition, ...] = DEFAULT_RULES,
) -> dict[str, Any]:
    comparison = None
    if facts.document_type == DocumentType.INVOICE and (order or notes):
        comparison = compare_invoice(facts, order, notes or [], TOL)
    elif facts.document_type == DocumentType.DELIVERY_NOTE and order:
        comparison = compare_delivery(facts, order, TOL)
    context = RuleContext(
        document=facts,
        reference_date=TODAY,
        comparison=comparison,
        duplicates=find_duplicates(facts, others or []),
        order_on_file=None if facts.po_reference is None else order is not None,
    )
    return {result.rule.code: result for result in evaluate(rules, context)}


def outcomes(found: dict[str, Any]) -> dict[str, Outcome]:
    return {code: result.outcome for code, result in found.items()}


def failing(found: dict[str, Any]) -> set[str]:
    return {code for code, result in found.items() if result.needs_review}


def test_clean_invoice_passes_every_rule() -> None:
    bill = invoice(ORDER, total="135.64")
    found = results(
        bill,
        order=purchase_order(ORDER, total="135.64"),
        notes=[delivery([line(0, "BRG-6204", 10), line(1, "VLV-BL050", 2)])],
    )
    assert failing(found) == set()
    assert outcomes(found)["INV_DELIVERED_QUANTITY"] == Outcome.PASS
    assert outcomes(found)["INV_PO_UNIT_PRICE"] == Outcome.PASS


def test_each_discrepancy_fails_its_rule() -> None:
    order = purchase_order(ORDER, total="135.64")
    note = delivery([line(0, "BRG-6204", 6), line(1, "VLV-BL050", 2)])
    price = results(invoice([ORDER[0], line(1, "VLV-BL050", 2, "41.47")], total="1"), order=order)
    assert failing(price) == {"INV_PO_UNIT_PRICE"}
    assert price["INV_PO_UNIT_PRICE"].items == ["line:VLV-BL050:unit_price"]
    assert "VLV-BL050" in price["INV_PO_UNIT_PRICE"].message

    quantity = results(invoice([line(0, "BRG-6204", 12, "4.85"), ORDER[1]], total="1"), order=order)
    assert failing(quantity) == {"INV_PO_QUANTITY"}

    partial = results(invoice([line(0, "BRG-6204", 8, "4.85"), ORDER[1]], total="1"), order=order)
    assert failing(partial) == set()  # invoicing less than ordered is allowed by default

    short = results(invoice(ORDER, total="1"), order=order, notes=[note])
    assert failing(short) == {"INV_DELIVERED_QUANTITY"}

    tax = results(invoice(ORDER, total="1", fields={"tax_rate": "0.1025"}), order=order)
    assert failing(tax) == {"INV_PO_TAX_RATE"}

    vendor = results(invoice(ORDER, total="1", vendor="Bluepeak", vendor_id="b"), order=order)
    assert {"INV_PO_VENDOR"} <= failing(vendor)
    assert vendor["INV_PO_VENDOR"].severity == Severity.CRITICAL


def test_missing_and_unknown_purchase_orders() -> None:
    no_reference = results(invoice(ORDER, po=None, total="1"))
    assert outcomes(no_reference)["INV_MISSING_PO"] == Outcome.FAIL
    assert outcomes(no_reference)["INV_PO_UNIT_PRICE"] == Outcome.NOT_APPLICABLE
    not_on_file = results(invoice(ORDER, total="1"))
    assert outcomes(not_on_file)["INV_MISSING_PO"] == Outcome.WARN
    assert "not on file" in not_on_file["INV_MISSING_PO"].message


def test_a_billed_line_on_no_delivery_note_exceeds_the_delivered_quantity() -> None:
    notes = [delivery([line(0, "BRG-6204", 10)])]
    found = results(invoice(ORDER), order=purchase_order(ORDER), notes=notes)
    assert outcomes(found)["INV_DELIVERED_QUANTITY"] == Outcome.FAIL
    assert (
        "VLV-BL050 is invoiced but on no delivery note" in found["INV_DELIVERED_QUANTITY"].message
    )
    unread = results(invoice(ORDER), order=purchase_order(ORDER), notes=[delivery([])])
    assert outcomes(unread)["INV_DELIVERED_QUANTITY"] == Outcome.WARN


def test_unread_order_lines_warn_instead_of_failing() -> None:
    found = results(invoice(ORDER), order=purchase_order([]))
    assert outcomes(found)["INV_LINE_NOT_ORDERED"] == Outcome.WARN
    assert found["INV_LINE_NOT_ORDERED"].message.startswith("Could not be verified")


def test_duplicates_fail_or_warn() -> None:
    first = invoice(ORDER, number="INV-7", total="125.30", created_minutes=0)
    again = invoice(ORDER, number="INV-7", total="125.30", created_minutes=1)
    found = results(again, others=[first], order=purchase_order(ORDER))
    assert outcomes(found)["INV_DUPLICATE"] == Outcome.FAIL
    assert found["INV_DUPLICATE"].evidence["duplicates"][0]["kind"] == "SAME_VENDOR_AND_NUMBER"
    possible = invoice(ORDER, number="INV-8", total="125.30", created_minutes=2)
    assert outcomes(results(possible, others=[first]))["INV_DUPLICATE"] == Outcome.WARN


def test_low_confidence_differences_warn_instead_of_failing() -> None:
    weak = invoice([line(0, "BRG-6204", 10, "4.65", confidence=0.7)], total="1")
    found = results(weak, order=purchase_order(ORDER[:1]))
    assert outcomes(found)["INV_PO_UNIT_PRICE"] == Outcome.WARN
    assert found["INV_PO_UNIT_PRICE"].message.startswith("Could not be verified")


def test_single_document_rules() -> None:
    broken = invoice(
        ORDER,
        total="1",
        checks=[
            {"code": "TOTAL_ARITHMETIC", "status": "FAIL", "message": "subtotal + tax = total"}
        ],
    )
    assert outcomes(results(broken))["DOC_ARITHMETIC"] == Outcome.FAIL

    # The same failed sum over a line amount read with low confidence may be a misread.
    lines_sum = {
        "code": "LINES_SUM",
        "status": "FAIL",
        "fields": ["line_items[0].amount", "total"],
        "message": "sum of line amounts = subtotal",
    }
    misread = invoice(
        [line(0, "BRG-6204", 10, "4.85", confidence=0.5)], total="1", checks=[lines_sum]
    )
    found = results(misread)
    assert outcomes(found)["DOC_ARITHMETIC"] == Outcome.WARN
    assert found["DOC_ARITHMETIC"].message.startswith("Could not be verified")
    confirmed = invoice([line(0, "BRG-6204", 10, "4.85")], total="1", checks=[lines_sum])
    assert outcomes(results(confirmed))["DOC_ARITHMETIC"] == Outcome.FAIL

    no_currency = invoice(ORDER, total="1", currency=None)
    found = results(no_currency)
    assert found["DOC_MANDATORY_FIELDS"].evidence == {"missing": ["currency"]}

    unknown = results(invoice(ORDER, total="1", vendor="Acme Widgets", vendor_id=None))
    assert outcomes(unknown)["DOC_UNKNOWN_VENDOR"] == Outcome.WARN

    long_terms = results(invoice(ORDER, total="1", fields={"payment_terms_days": 90}))
    assert outcomes(long_terms)["POLICY_PAYMENT_TERMS"] == Outcome.FAIL


@pytest.mark.parametrize(
    ("expires", "renews", "expected"),
    [
        ("2026-03-01", False, Outcome.FAIL),
        ("2026-03-01", True, Outcome.WARN),
        ("2026-04-01", False, Outcome.WARN),
        ("2027-01-01", False, Outcome.PASS),
        (None, False, Outcome.NOT_APPLICABLE),
    ],
)
def test_contract_expiry(expires: str | None, renews: bool, expected: Outcome) -> None:
    contract = document(
        DocumentType.CONTRACT,
        number="MSA-1",
        vendor=None,
        vendor_id=None,
        currency=None,
        fields={
            "party_a": "Kestrel",
            "party_b": "Meridian",
            "expiration_date": expires,
            "auto_renewal": renews,
        },
    )
    assert outcomes(results(contract))["CONTRACT_EXPIRY"] == expected


def test_delivery_note_rules() -> None:
    order = purchase_order(ORDER)
    over = results(delivery([line(0, "BRG-6204", 12), line(1, "VLV-BL050", 2)]), order=order)
    assert failing(over) == {"DN_PO_QUANTITY"}
    short = results(delivery([line(0, "BRG-6204", 6)]), order=order)
    assert failing(short) == set()


def test_disabled_rules_do_not_run() -> None:
    rules = tuple(replace(rule, enabled=rule.code != "INV_MISSING_PO") for rule in DEFAULT_RULES)
    assert "INV_MISSING_PO" not in results(invoice(ORDER, po=None, total="1"), rules=rules)


def test_parameters_are_validated() -> None:
    assert validate_params("line_unit_price", {"tolerance_pct": "0.02"}) == {
        "tolerance_pct": "0.02",
        "tolerance_abs": "0.01",
    }
    with pytest.raises(InvalidRuleParamsError, match="tolerance_pct"):
        validate_params("line_unit_price", {"tolerance_pct": "-1"})
    with pytest.raises(InvalidRuleParamsError, match="Extra inputs"):
        validate_params("line_unit_price", {"tolerence": "1"})
    with pytest.raises(InvalidRuleParamsError, match="no field"):
        validate_params("mandatory_fields", {"fields": {"INVOICE": ["shoe_size"]}})
    with pytest.raises(InvalidRuleParamsError, match="unknown rule type"):
        validate_params("telepathy", {})
    with pytest.raises(InvalidRuleParamsError, match="check"):
        validate_params("header_match", {"check": "colour"})
    # Every default rule's parameters are valid for its type.
    for rule in DEFAULT_RULES:
        validate_params(rule.rule_type, rule.params)


class _Exploding:
    rule_type: ClassVar[str] = "test_exploding"
    params_model: ClassVar[type[RuleParams]] = RuleParams

    def evaluate(self, context: RuleContext, params: Any) -> Finding:
        raise ZeroDivisionError


def test_a_broken_rule_is_reported_as_error_and_the_others_still_run() -> None:
    if "test_exploding" not in rule_types():
        register(_Exploding())
    broken = RuleDefinition(
        "BROKEN", "test_exploding", "Broken", "", frozenset({DocumentType.INVOICE}), Severity.LOW
    )
    found = results(invoice(ORDER, total="1"), rules=(*DEFAULT_RULES, broken))
    assert found["BROKEN"].outcome == Outcome.ERROR
    assert "ZeroDivisionError" in found["BROKEN"].message
    assert found["BROKEN"].needs_review
    assert "INV_MISSING_PO" in found


def test_tolerances_come_from_the_rules() -> None:
    assert Tolerances(min_confidence=0.85) == TOL
    lenient = tuple(
        replace(rule, params={"tolerance_pct": "0.05", "tolerance_abs": "0"})
        if rule.code == "INV_PO_UNIT_PRICE"
        else rule
        for rule in DEFAULT_RULES
    )
    tolerances = tolerances_from_rules(lenient, min_confidence=0.9)
    assert (tolerances.price_pct, tolerances.price_abs) == (Decimal("0.05"), Decimal("0"))
    assert duplicate_params(DEFAULT_RULES) == {"date_window_days": 7, "match_amount_and_date": True}


# ------------------------------------------------------------------------------ real documents
async def _bundle_facts(tmp_path: Path, scenario: str) -> dict[str, DocumentFacts]:
    from docintel.documents.validation import FileKind
    from docintel.evaluation.common import extract_file
    from docintel.evaluation.extraction_suite import demo_vendor_records
    from docintel.fields.service import ExtractionPolicy, ExtractionRequest, FieldExtractionService
    from docintel.fields.vendors import StaticVendorDirectory
    from docintel.processing.extraction import ExtractionOptions
    from docintel.processing.tables import stitch_tables
    from docintel.synthetic.generator import generate_dataset
    from docintel.synthetic.scenarios import Scenario
    from tests.unit.test_extraction import FakeOCR

    manifest = generate_dataset(tmp_path, seed=5, scenarios=[Scenario(scenario)])
    service = FieldExtractionService(
        policy=ExtractionPolicy(llm_mode="never"),
        vendors=StaticVendorDirectory(demo_vendor_records()),
    )
    facts: dict[str, DocumentFacts] = {}
    for minute, entry in enumerate(manifest["documents"]):
        truth = json.loads((tmp_path / entry["ground_truth"]).read_text())
        doc_type = DocumentType(truth["document_type"])
        pages = await extract_file(
            tmp_path / entry["file"], FileKind.PDF, FakeOCR(), ExtractionOptions()
        )
        outcome = await service.extract(ExtractionRequest(doc_type, pages, stitch_tables(pages)))
        assert outcome is not None
        facts[truth["doc_id"].split("-", 1)[1]] = facts_from_fields(
            doc_type,
            outcome.fields,
            checks=[check.to_json() for check in outcome.scoring.checks],
            document_id=uuid.uuid4(),
            label=entry["file"],
            created_at=datetime(2026, 3, 1, tzinfo=UTC) + timedelta(minutes=minute),
        )
    return facts


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("CLEAN_MATCH", set()),
        ("UNIT_PRICE_MISMATCH", {"INV_PO_UNIT_PRICE"}),
        ("QUANTITY_MISMATCH", {"INV_PO_QUANTITY", "INV_DELIVERED_QUANTITY"}),
        ("SHORT_DELIVERY", {"INV_DELIVERED_QUANTITY"}),
        ("TAX_RATE_MISMATCH", {"INV_PO_TAX_RATE"}),
        ("MISSING_PO_REFERENCE", {"INV_MISSING_PO"}),
        ("TOTAL_ARITHMETIC_ERROR", {"DOC_ARITHMETIC"}),
    ],
)
async def test_generated_bundles_raise_exactly_their_discrepancy(
    tmp_path: Path, scenario: str, expected: set[str]
) -> None:
    facts = await _bundle_facts(tmp_path, scenario)
    order, note, bill = facts["PO"], facts["DN"], facts["INV"]
    found = results(bill, order=order if bill.po_reference else None, notes=[note])
    assert failing(found) == expected
    # Native documents are read reliably: every planted discrepancy is confirmed, not "could
    # not be verified" (a failed consistency check must not count as a weak reading).
    assert {code: found[code].outcome for code in expected} == dict.fromkeys(expected, Outcome.FAIL)
    assert failing(results(note, order=order)) == set()
    assert failing(results(order)) == set()


async def test_a_resent_invoice_is_a_duplicate(tmp_path: Path) -> None:
    facts = await _bundle_facts(tmp_path, "DUPLICATE_INVOICE")
    first, resent = facts["INV"], facts["INV2"]
    found = results(resent, order=facts["PO"], notes=[facts["DN"]], others=[first])
    assert failing(found) == {"INV_DUPLICATE"}
    assert failing(results(first, order=facts["PO"], notes=[facts["DN"]], others=[resent])) == set()


# ------------------------------------------------------------------------------ contract clauses
def contract(*clauses: tuple[str, str]) -> DocumentFacts:
    return DocumentFacts(
        DocumentType.CONTRACT,
        {},
        [],
        clauses=[ClauseFact(str(i + 1), title, body, 1) for i, (title, body) in enumerate(clauses)],
    )


TERMINATION = ("Term and Termination", "Either Party may terminate upon 60 days written notice.")
LIABILITY = ("Limitation of Liability", "Liability shall not exceed 500,000 USD.")
OHIO = ("Governing Law", "This Agreement is governed by the laws of the State of Ohio.")
CLAUSE_RULES = (
    "CONTRACT_REQUIRED_CLAUSES",
    "CONTRACT_TERMINATION_NOTICE",
    "CONTRACT_GOVERNING_LAW",
)


def clause_outcomes(facts: DocumentFacts) -> dict[str, tuple[str, str]]:
    found = results(facts)
    return {code: (found[code].outcome.value, found[code].message) for code in CLAUSE_RULES}


def test_a_contract_with_the_required_clauses_passes() -> None:
    outcomes = clause_outcomes(contract(TERMINATION, LIABILITY, OHIO))
    assert {code: outcome for code, (outcome, _) in outcomes.items()} == dict.fromkeys(
        CLAUSE_RULES, "PASS"
    )
    assert "60 days" in outcomes["CONTRACT_TERMINATION_NOTICE"][1]


def test_contract_deviations_from_the_guidelines() -> None:
    long_notice = ("Term and Termination", "Termination requires 180 days' prior written notice.")
    english = ("Governing Law", "Governed by the laws of England and Wales.")
    outcomes = clause_outcomes(contract(long_notice, english))
    assert outcomes["CONTRACT_REQUIRED_CLAUSES"] == (
        "FAIL",
        "Required clause(s) missing: Liability.",
    )
    assert outcomes["CONTRACT_TERMINATION_NOTICE"][0] == "FAIL"
    assert "180 days exceeds the maximum of 90" in outcomes["CONTRACT_TERMINATION_NOTICE"][1]
    assert outcomes["CONTRACT_GOVERNING_LAW"][0] == "WARN"
    assert "England and Wales, not Ohio" in outcomes["CONTRACT_GOVERNING_LAW"][1]


def test_clause_rules_without_clauses_or_numbers() -> None:
    unknown = clause_outcomes(DocumentFacts(DocumentType.CONTRACT, {}, []))
    assert {code: outcome for code, (outcome, _) in unknown.items()} == dict.fromkeys(
        CLAUSE_RULES, "NOT_APPLICABLE"
    )  # the text was not loaded (e.g. facts built from fields only)
    vague = clause_outcomes(
        contract(("Term and Termination", "Either Party may terminate on reasonable notice."))
    )
    assert vague["CONTRACT_TERMINATION_NOTICE"][0] == "WARN"
    assert vague["CONTRACT_GOVERNING_LAW"][0] == "NOT_APPLICABLE"  # missing: the first rule
    assert clause_outcomes(contract())["CONTRACT_REQUIRED_CLAUSES"][0] == "WARN"


def test_an_extracted_notice_period_wins_over_the_clause_text() -> None:
    facts = contract(TERMINATION, LIABILITY, OHIO)
    facts.fields["termination_notice_days"] = FactValue(
        path="termination_notice_days",
        name="termination_notice_days",
        value_type=ValueType.DAYS,
        value=120,
        display="120 days",
        page=2,
        source_text="120 days",
        bbox=None,
        confidence=0.95,
        evidence="VERIFIED",
    )
    outcome, message = clause_outcomes(facts)["CONTRACT_TERMINATION_NOTICE"]
    assert (outcome, "120 days" in message) == ("FAIL", True)


def test_generated_contracts_carry_their_clauses_through_the_rules() -> None:
    rng = random.Random(4)
    for index in range(1, 8):
        versions, _ = contract_family(rng, index)
        for version in versions:
            titles = [title for title, _ in version.clauses]
            facts = contract(*version.clauses)
            outcomes = {code: found[0] for code, found in clause_outcomes(facts).items()}
            complete = all(
                title in titles
                for title in ("Term and Termination", "Limitation of Liability", "Governing Law")
            )
            assert outcomes["CONTRACT_REQUIRED_CLAUSES"] == ("PASS" if complete else "FAIL")
            # The generator's ground truth for the evaluation agrees with the rules.
            truth = version.guidelines()
            assert bool(truth["missing_required"]) == (not complete)
            notice = truth["termination_notice_days"]
            assert outcomes["CONTRACT_TERMINATION_NOTICE"] == (
                "NOT_APPLICABLE" if notice is None else "FAIL" if notice > 90 else "PASS"
            )
            law = truth["governing_law"]
            assert outcomes["CONTRACT_GOVERNING_LAW"] == (
                "NOT_APPLICABLE" if law is None else "PASS" if "Ohio" in law else "WARN"
            )

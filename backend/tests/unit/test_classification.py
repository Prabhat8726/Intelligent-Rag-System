"""Classification: corpus, local model, LLM fallback with agreement confidence, sensitivity gate."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from docintel.ai.base import LLMRequest, LLMResponse, LLMUsage, ModelTier, StructuredLLMResponse
from docintel.ai.errors import ProviderRateLimitError
from docintel.ai.routing import ExternalAIGate, max_sensitivity
from docintel.classification.corpus import generate_corpus, generate_document, ocr_noise
from docintel.classification.model import LocalClassifier, LocalPrediction, normalize_text
from docintel.classification.service import (
    DocumentClassifier,
    LLMClassification,
    agreement_confidence,
    keyword_evidence,
)
from docintel.db.models import ClassificationMethod, DocumentType, ReviewReason, Sensitivity
from docintel.processing.sensitivity import (
    SensitivityAssessment,
    assess_pages,
    iban_valid,
    luhn_valid,
)
from docintel.processing.services import train_classifier

INVOICE_TEXT = (
    "Kestrel Industrial Supply Inc.\n\nINVOICE\n\nInvoice No.  INV-KIS-2571945\n"
    "Invoice Date  07/28/2026\nBill To\nMeridian Manufacturing Co.\n\n"
    "# | Item | Description | Qty | Unit | Unit Price | Amount\n"
    "1 | BLT-M10 | Hex bolt | 5 | box | 14.31 | 71.55\n\nTotal Due  $ 71.55"
)


@pytest.fixture(scope="module")
def local() -> LocalClassifier:
    return train_classifier(per_class=60)


def test_corpus_is_deterministic_and_balanced() -> None:
    first = generate_corpus(seed=3, per_class=5)
    assert first == generate_corpus(seed=3, per_class=5)
    assert first != generate_corpus(seed=4, per_class=5)
    assert {sample.label for sample in first} == set(DocumentType)
    assert all(len(sample.text) > 40 for sample in first)


def test_corpus_uses_only_reserved_example_domains() -> None:
    import random
    import re

    texts = [
        generate_document(doc_type, random.Random(i), noise=False)
        for doc_type in (DocumentType.RESUME, DocumentType.OTHER)
        for i in range(30)
    ]
    domains = {match for text in texts for match in re.findall(r"@([\w.]+)", text)}
    assert domains <= {"example.com", "example.org", "example.net"}


def test_ocr_noise_changes_some_characters() -> None:
    import random

    text = "Invoice total amount due on receipt " * 5
    noisy = ocr_noise(text, random.Random(1), rate=0.2)
    assert noisy != text
    assert ocr_noise(text, random.Random(1), rate=0.0) == text


def test_normalization_hides_numbers() -> None:
    assert normalize_text("Invoice  INV-2026\n Total 1,250.00") == "invoice inv-0000 total 0,000.00"


def test_local_model_classifies_held_out_samples(local: LocalClassifier) -> None:
    held_out = generate_corpus(seed=99, per_class=10)
    predictions = local.predict_many([sample.text for sample in held_out])
    accuracy = sum(p.label == s.label for p, s in zip(predictions, held_out, strict=True)) / len(
        held_out
    )
    assert accuracy >= 0.9
    first = predictions[0]
    assert sum(first.probabilities.values()) == pytest.approx(1.0, abs=1e-6)
    assert local.fingerprint.startswith("tfidf-lr-v1:")
    assert local.predict(INVOICE_TEXT).label == DocumentType.INVOICE


def test_training_is_deterministic(local: LocalClassifier) -> None:
    again = LocalClassifier.train(generate_corpus(seed=1, per_class=60))
    assert again.fingerprint == local.fingerprint
    assert again.predict(INVOICE_TEXT).probabilities == local.predict(INVOICE_TEXT).probabilities


# ------------------------------------------------------------------------------ LLM fallback
class FakeLLM:
    def __init__(
        self, label: DocumentType | None = None, quote: str = "", error: bool = False
    ) -> None:
        self.label = label
        self.quote = quote
        self.error = error
        self.requests: list[LLMRequest] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def supports_images(self) -> bool:
        return False

    @property
    def local(self) -> bool:
        return False

    def model_for(self, tier: ModelTier) -> str:
        return f"fake-{tier.value}"

    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredLLMResponse[T]:
        self.requests.append(request)
        if self.error:
            raise ProviderRateLimitError("quota", provider="fake")
        data = schema.model_validate({"document_type": self.label, "evidence_quote": self.quote})
        usage = LLMUsage(provider="fake", model="fake-fast", latency_ms=12.0)
        return StructuredLLMResponse(data=data, raw_text="{}", usage=usage)

    async def aclose(self) -> None:
        return None


def classifier(
    local: LocalClassifier,
    llm: FakeLLM | None,
    *,
    min_confidence: float = 0.7,
    max_sensitivity: Sensitivity = Sensitivity.INTERNAL,
) -> DocumentClassifier:
    gate = ExternalAIGate(max_sensitivity=max_sensitivity, provider_configured=llm is not None)
    return DocumentClassifier(local=local, gate=gate, llm=llm, min_confidence=min_confidence)


async def test_confident_local_prediction_needs_no_llm(local: LocalClassifier) -> None:
    llm = FakeLLM(DocumentType.OTHER)
    outcome = await classifier(local, llm).classify(
        INVOICE_TEXT, declared=Sensitivity.INTERNAL, assessment=SensitivityAssessment()
    )
    assert outcome.label == DocumentType.INVOICE
    assert outcome.method == ClassificationMethod.LOCAL_MODEL
    assert outcome.review_reason is None
    assert outcome.model_version == local.fingerprint
    assert outcome.signals["local"][0]["label"] == "INVOICE"
    assert "invoice" in outcome.signals["keywords"]["INVOICE"]
    assert llm.requests == []


async def test_no_text_goes_to_review(local: LocalClassifier) -> None:
    outcome = await classifier(local, None).classify(
        "  12 \n", declared=Sensitivity.INTERNAL, assessment=SensitivityAssessment()
    )
    assert outcome.label is None
    assert outcome.review_reason == ReviewReason.NO_TEXT_FOUND


async def test_uncertain_without_llm_goes_to_review(local: LocalClassifier) -> None:
    outcome = await classifier(local, None, min_confidence=1.01).classify(
        INVOICE_TEXT, declared=Sensitivity.INTERNAL, assessment=SensitivityAssessment()
    )
    assert outcome.label == DocumentType.INVOICE  # best local guess is kept for the reviewer
    assert outcome.review_reason == ReviewReason.CLASSIFICATION_UNCERTAIN
    assert outcome.signals["llm"] == {"used": False, "reason": "no external AI provider configured"}


async def test_gate_blocks_confidential_documents(local: LocalClassifier) -> None:
    llm = FakeLLM(DocumentType.INVOICE, quote="INVOICE")
    outcome = await classifier(local, llm, min_confidence=1.01).classify(
        INVOICE_TEXT, declared=Sensitivity.CONFIDENTIAL, assessment=SensitivityAssessment()
    )
    assert llm.requests == []
    assert outcome.review_reason == ReviewReason.CLASSIFICATION_UNCERTAIN
    assert outcome.signals["external_ai"]["allowed"] is False
    assert "AI_EXTERNAL_MAX_SENSITIVITY=INTERNAL" in outcome.signals["llm"]["reason"]


async def test_content_findings_raise_the_effective_sensitivity(local: LocalClassifier) -> None:
    llm = FakeLLM(DocumentType.INVOICE, quote="INVOICE")
    assessment = assess_pages([(1, INVOICE_TEXT + "\nCard 4111 1111 1111 1111")])
    outcome = await classifier(local, llm, min_confidence=1.01).classify(
        INVOICE_TEXT, declared=Sensitivity.PUBLIC, assessment=assessment
    )
    assert llm.requests == []
    assert outcome.signals["external_ai"]["effective_sensitivity"] == "RESTRICTED"


async def test_llm_agreeing_with_local_model_is_an_ensemble(local: LocalClassifier) -> None:
    llm = FakeLLM(DocumentType.INVOICE, quote="Invoice No. INV-KIS-2571945")
    outcome = await classifier(local, llm, min_confidence=1.01).classify(
        INVOICE_TEXT, declared=Sensitivity.INTERNAL, assessment=SensitivityAssessment()
    )
    request = llm.requests[0]
    assert request.tier == ModelTier.FAST
    assert "<document>" in request.prompt
    assert "untrusted" in (request.system_instruction or "")
    assert outcome.method == ClassificationMethod.ENSEMBLE
    assert outcome.label == DocumentType.INVOICE
    assert outcome.signals["llm"]["quote_found"] is True
    # confidence comes from agreement (>= 0.80 + keyword bonus), never from the LLM itself
    assert 0.9 <= outcome.confidence <= 0.95
    assert outcome.review_reason == ReviewReason.CLASSIFICATION_UNCERTAIN  # threshold 1.01


async def test_llm_disagreeing_without_evidence_is_low_confidence(local: LocalClassifier) -> None:
    llm = FakeLLM(DocumentType.RESUME, quote="ten years of leadership experience")
    outcome = await classifier(local, llm, min_confidence=0.99).classify(
        INVOICE_TEXT, declared=Sensitivity.INTERNAL, assessment=SensitivityAssessment()
    )
    assert outcome.method == ClassificationMethod.LLM
    assert outcome.label == DocumentType.RESUME
    assert outcome.signals["llm"]["quote_found"] is False  # hallucinated quote is penalized
    assert outcome.confidence <= 0.35
    assert outcome.review_reason == ReviewReason.CLASSIFICATION_UNCERTAIN


async def test_provider_errors_degrade_to_review(local: LocalClassifier) -> None:
    outcome = await classifier(local, FakeLLM(error=True), min_confidence=1.01).classify(
        INVOICE_TEXT, declared=Sensitivity.INTERNAL, assessment=SensitivityAssessment()
    )
    assert outcome.review_reason == ReviewReason.CLASSIFICATION_UNCERTAIN
    assert outcome.signals["llm"] == {
        "used": False,
        "reason": "provider error: ProviderRateLimitError",
    }


def test_agreement_confidence_rules() -> None:
    local = LocalPrediction(
        {DocumentType.INVOICE: 0.55, DocumentType.RECEIPT: 0.35, DocumentType.OTHER: 0.10}
    )
    evidence: dict[DocumentType, list[str]] = {DocumentType.RECEIPT: ["receipt", "change"]}
    assert agreement_confidence(DocumentType.INVOICE, local, {}, True) == (
        0.80,
        ClassificationMethod.ENSEMBLE,
    )
    assert agreement_confidence(DocumentType.RECEIPT, local, evidence, True) == (
        0.75,
        ClassificationMethod.LLM,
    )
    assert agreement_confidence(DocumentType.OTHER, local, evidence, False) == (
        0.25,
        ClassificationMethod.LLM,
    )


def test_keyword_evidence_matches_whole_phrases() -> None:
    evidence = keyword_evidence("Purchase Order PO Number 12\nchanged items")
    assert evidence[DocumentType.PURCHASE_ORDER] == ["purchase order", "po number"]
    assert DocumentType.RECEIPT not in evidence  # "changed" is not "change"


def test_llm_schema_rejects_unknown_types() -> None:
    with pytest.raises(ValueError, match="document_type"):
        LLMClassification.model_validate({"document_type": "MEME", "evidence_quote": "x"})


# ------------------------------------------------------------------------------ sensitivity
def test_gate_decisions() -> None:
    gate = ExternalAIGate(max_sensitivity=Sensitivity.INTERNAL, provider_configured=True)
    assert gate.decide(Sensitivity.PUBLIC).allowed
    assert gate.decide(Sensitivity.INTERNAL, None).allowed
    blocked = gate.decide(Sensitivity.INTERNAL, Sensitivity.CONFIDENTIAL)
    assert not blocked.allowed
    assert blocked.effective_sensitivity == Sensitivity.CONFIDENTIAL
    unconfigured = ExternalAIGate(max_sensitivity=Sensitivity.RESTRICTED, provider_configured=False)
    assert not unconfigured.decide(Sensitivity.PUBLIC).allowed
    assert max_sensitivity() == Sensitivity.PUBLIC


@pytest.mark.parametrize(
    ("text", "kinds", "detected"),
    [
        ("Card 4111 1111 1111 1111 expires", {"PAYMENT_CARD": 1}, Sensitivity.RESTRICTED),
        ("Card 4111 1111 1111 1112", {}, None),  # Luhn-invalid
        ("Order 1234567890123 shipped", {}, None),  # 13 digits, Luhn-invalid
        ("SSN 123-45-6789 on file", {"US_SSN": 1}, Sensitivity.RESTRICTED),
        ("Ref 000-12-3456 and 666-12-3456", {}, None),  # invalid SSN areas
        ("IBAN DE89 3704 0044 0532 0130 00 please", {"IBAN": 1}, None),
        ("IBAN XX00 0000 KIS 0000 0000 please", {}, None),  # synthetic, mod-97 invalid
        ("mail avery.park@example.com", {"EMAIL": 1}, None),
    ],
)
def test_sensitive_content_detection(
    text: str, kinds: dict[str, int], detected: Sensitivity | None
) -> None:
    assessment = assess_pages([(2, text)])
    assert {f.kind: f.count for f in assessment.findings} == kinds
    assert assessment.detected == detected
    serialized: dict[str, Any] = assessment.to_json()
    assert "4111" not in str(serialized)  # counts only, never the values
    if kinds:
        assert assessment.findings[0].pages == [2]


def test_type_minimum_applies_after_classification() -> None:
    assessment = assess_pages([(1, "Experience and education")])
    assert assessment.detected is None
    assert assessment.with_type(DocumentType.RESUME).detected == Sensitivity.CONFIDENTIAL
    assert assessment.with_type(DocumentType.INVOICE).detected is None


def test_checksums() -> None:
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")
    assert iban_valid("GB82 WEST 1234 5698 7654 32")
    assert not iban_valid("GB82 WEST 1234 5698 7654 33")

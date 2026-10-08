"""Engines the processing pipeline uses, built once per worker process.

The local classifier is trained at worker start (ADR-022) from the seeded synthetic corpus plus
human corrections stored in the database. Training on the corpus alone is deterministic and
cached per process, so tests and `--until-idle` runs pay for it once.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import Text, func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.ai.base import LLMProvider
from docintel.ai.registry import build_llm_provider
from docintel.ai.routing import ExternalAIGate
from docintel.classification.corpus import LabelledText, generate_corpus
from docintel.classification.model import LocalClassifier
from docintel.classification.service import DocumentClassifier
from docintel.core.config import Settings
from docintel.core.logging import get_logger
from docintel.db.models import (
    ClassificationMethod,
    Document,
    DocumentClassification,
    DocumentPage,
    Sensitivity,
)
from docintel.processing.extraction import ExtractionOptions
from docintel.processing.ocr import OCRProvider, TesseractOCRProvider

logger = get_logger(__name__)

CORPUS_SEED = 1  # training corpus; evaluation uses other seeds


@lru_cache(maxsize=4)
def _corpus_classifier(per_class: int) -> LocalClassifier:
    return LocalClassifier.train(generate_corpus(seed=CORPUS_SEED, per_class=per_class))


def train_classifier(per_class: int, corrections: Sequence[LabelledText] = ()) -> LocalClassifier:
    if not corrections:
        return _corpus_classifier(per_class)
    corpus = generate_corpus(seed=CORPUS_SEED, per_class=per_class)
    return LocalClassifier.train([*corpus, *corrections])


async def load_corrections(session: AsyncSession) -> list[LabelledText]:
    """Current human labels with the text of the version they were given for."""
    separator = literal("\n\n", type_=Text)
    page_text = (
        select(
            func.string_agg(
                DocumentPage.text, aggregate_order_by(separator, DocumentPage.page_number)
            )
        )
        .where(DocumentPage.document_version_id == DocumentClassification.document_version_id)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(DocumentClassification.label, page_text)
        .join(Document, Document.id == DocumentClassification.document_id)
        .where(
            DocumentClassification.is_current.is_(True),
            DocumentClassification.method == ClassificationMethod.HUMAN,
            Document.deleted_at.is_(None),
        )
    )
    return [LabelledText(text, label) for label, text in rows if text and text.strip()]


@dataclass(slots=True)
class ProcessingServices:
    ocr: OCRProvider
    classifier: DocumentClassifier
    extraction: ExtractionOptions
    ocr_review_below_confidence: float
    llm: LLMProvider | None = None

    async def aclose(self) -> None:
        if self.llm is not None:
            await self.llm.aclose()


def build_processing_services(
    settings: Settings,
    *,
    corrections: Sequence[LabelledText] = (),
    ocr: OCRProvider | None = None,
    llm: LLMProvider | None = None,
) -> ProcessingServices:
    if llm is None and settings.gemini_api_key is not None and settings.classification_llm_fallback:
        llm = build_llm_provider(settings)
    local = train_classifier(settings.classification_corpus_per_class, corrections)
    logger.info(
        "classifier.ready",
        model_version=local.fingerprint,
        training_size=local.training_size,
        corrections=len(corrections),
        training_seconds=round(local.training_seconds, 2),
    )
    gate = ExternalAIGate(
        max_sensitivity=Sensitivity(settings.ai_external_max_sensitivity),
        provider_configured=llm is not None,
    )
    classifier = DocumentClassifier(
        local=local,
        gate=gate,
        llm=llm,
        min_confidence=settings.classification_min_confidence,
        llm_fallback_enabled=settings.classification_llm_fallback,
    )
    return ProcessingServices(
        ocr=ocr
        or TesseractOCRProvider(
            command=settings.tesseract_cmd,
            languages=settings.ocr_languages,
            timeout_seconds=settings.ocr_page_timeout_seconds,
        ),
        classifier=classifier,
        extraction=ExtractionOptions(
            ocr_dpi=settings.ocr_dpi,
            upscale_below_dpi=settings.ocr_upscale_below_dpi,
            preview_width=settings.page_preview_width,
            ocr_concurrency=settings.ocr_concurrency,
            remove_ruling_lines=settings.ocr_remove_ruling_lines,
        ),
        ocr_review_below_confidence=settings.ocr_review_below_confidence,
        llm=llm,
    )

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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.ai.accounting import AccountedLLMProvider, DatabaseLLMCallLog
from docintel.ai.base import LLMProvider
from docintel.ai.registry import build_llm_provider, model_prices
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
from docintel.fields.llm import ExtractionCache
from docintel.fields.service import FieldExtractionService, policy_from_settings
from docintel.fields.store import DatabaseExtractionCache
from docintel.fields.vendors import DatabaseVendorDirectory, StaticVendorDirectory, VendorDirectory
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
    fields: FieldExtractionService
    llm: LLMProvider | None = None

    async def aclose(self) -> None:
        if self.llm is not None:
            await self.llm.aclose()


def build_processing_services(
    settings: Settings,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    corrections: Sequence[LabelledText] = (),
    ocr: OCRProvider | None = None,
    llm: LLMProvider | None = None,
) -> ProcessingServices:
    """Engines for the pipeline. With a sessionmaker, LLM calls are accounted in `llm_calls`
    (and the daily budget enforced), vendors resolve against the database and identical model
    inputs reuse stored outputs."""
    wants_llm = settings.classification_llm_fallback or settings.extraction_llm_mode != "never"
    if llm is None and settings.llm_configured and wants_llm:
        llm = build_llm_provider(settings)
    if llm is not None and sessionmaker is not None:
        log = DatabaseLLMCallLog(
            sessionmaker, daily_request_budget=settings.llm_daily_request_budget
        )
        llm = AccountedLLMProvider(llm, log, model_prices(settings))
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
        provider_local=llm.local if llm is not None else False,
    )
    vendors: VendorDirectory
    cache: ExtractionCache | None
    if sessionmaker is not None:
        vendors = DatabaseVendorDirectory(sessionmaker, settings.vendor_match_min_score)
        cache = DatabaseExtractionCache(sessionmaker)
    else:
        vendors, cache = StaticVendorDirectory(), None
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
        fields=FieldExtractionService(
            policy=policy_from_settings(settings),
            llm=llm,
            gate=gate,
            vendors=vendors,
            cache=cache,
            max_prompt_chars=settings.extraction_max_prompt_chars,
            max_output_tokens=settings.extraction_max_output_tokens,
            max_images=settings.extraction_max_images,
        ),
        llm=llm,
    )

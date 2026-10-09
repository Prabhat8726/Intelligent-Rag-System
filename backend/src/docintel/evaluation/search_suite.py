"""Search suite: natural-language search over business documents (Modules 25, 28).

A synthetic dataset (native PDFs: purchase orders, invoices, delivery notes from five vendors
with 30-, 45- and 60-day payment terms) is uploaded with DocumentService and processed by the
worker - classification, extraction, vendor resolution, indexing - in a scratch database.
Questions are generated from the ground truth and run through DocumentSearchService, the code
behind POST /api/v1/search. Each family is scored against the documents its ground truth
selects, so the numbers measure parsing, extraction and filtering together:

* vendor:   "invoices from <vendor>", "purchase orders from <spelling variant>",
            "documents from <first two words>"
* terms:    "documents with payment terms longer than / at least / shorter than N days"
* totals:   "invoices over N", "purchase orders under N"
* dates:    "invoices in <month year>"
* types:    "delivery notes"
* text:     a line-item description; relevant = documents listing that item (recall@10 and
            precision@10, since ranked text search returns near matches too)
"""

from __future__ import annotations

import calendar
import io
import json
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.datastructures import Headers

from docintel.ai.local_embeddings import HASHING_MODEL, HashingEmbeddingProvider
from docintel.audit.service import SYSTEM_REQUEST
from docintel.db.models import Department, Role, Sensitivity, User
from docintel.documents.service import DocumentService
from docintel.evaluation.metrics import counts_prf
from docintel.evaluation.report import Report, environment, pct
from docintel.evaluation.retrieval_suite import _settings, scratch_database
from docintel.knowledge.embedding import ChunkEmbedder
from docintel.processing.services import build_processing_services
from docintel.search.service import DocumentSearchService
from docintel.storage import LocalStorage
from docintel.synthetic.catalog import VENDORS
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.scenarios import Scenario
from docintel.vendors.seed import seed_demo_vendors
from docintel.workers.runner import Worker

DATASET_SEED = 2  # bundles from all five catalog vendors
SCENARIOS = [
    Scenario.CLEAN_MATCH,
    Scenario.VENDOR_NAME_VARIANT,
    Scenario.UNIT_PRICE_MISMATCH,
    Scenario.QUANTITY_MISMATCH,
    Scenario.SHORT_DELIVERY,
    Scenario.TAX_RATE_MISMATCH,
]
TEXT_DEPTH = 10

Truth = dict[str, Any]


def _fields(truth: Truth) -> dict[str, Any]:
    fields: dict[str, Any] = truth["fields"]
    return fields


def _vendor(code: str, kind: str | None) -> Callable[[Truth], bool]:
    def keep(truth: Truth) -> bool:
        return _fields(truth)["vendor_code"] == code and kind in (None, truth["document_type"])

    return keep


def _invoice_month(month: str) -> Callable[[Truth], bool]:
    def keep(truth: Truth) -> bool:
        issued: str = _fields(truth)["issue_date"]
        return truth["document_type"] == "INVOICE" and issued.startswith(month)

    return keep


def _lists_item(description: str) -> Callable[[Truth], bool]:
    def keep(truth: Truth) -> bool:
        return any(line["description"] == description for line in truth["line_items"])

    return keep


def build_questions(truths: dict[str, Truth]) -> list[dict[str, Any]]:
    """(family, question, predicate over ground truth) from the documents in the dataset."""
    questions: list[dict[str, Any]] = []

    def add(family: str, question: str, keep: Callable[[Truth], bool]) -> None:
        relevant = sorted(doc for doc, truth in truths.items() if keep(truth))
        questions.append({"family": family, "question": question, "relevant": relevant})

    present = {_fields(t)["vendor_code"] for t in truths.values()}
    for vendor in VENDORS:
        if vendor.code not in present:
            continue
        add("vendor", f"invoices from {vendor.name}", _vendor(vendor.code, "INVOICE"))
        add(
            "vendor",
            f"purchase orders from {vendor.name_variants[0]}",
            _vendor(vendor.code, "PURCHASE_ORDER"),
        )
        short = " ".join(vendor.name.split()[:2])
        add("vendor", f"documents from {short}", _vendor(vendor.code, None))

    def terms(t: Truth) -> int | None:
        value = _fields(t).get("payment_terms_days")
        return int(value) if value is not None else None

    add(
        "payment terms",
        "documents with payment terms longer than 30 days",
        lambda t: (terms(t) or 0) > 30,
    )
    add(
        "payment terms",
        "documents with payment terms of at least 60 days",
        lambda t: (terms(t) or 0) >= 60,
    )
    add(
        "payment terms",
        "documents with payment terms shorter than 45 days",
        lambda t: terms(t) is not None and (terms(t) or 0) < 45,
    )

    def total(t: Truth) -> Decimal | None:
        value = _fields(t).get("total")
        return Decimal(str(value)) if value is not None else None

    add(
        "totals",
        "invoices over 2,000",
        lambda t: t["document_type"] == "INVOICE" and (total(t) or 0) > 2000,
    )
    add(
        "totals",
        "purchase orders under 1,000",
        lambda t: (
            t["document_type"] == "PURCHASE_ORDER"
            and total(t) is not None
            and (total(t) or 0) < 1000
        ),
    )

    months = sorted(
        {_fields(t)["issue_date"][:7] for t in truths.values() if t["document_type"] == "INVOICE"}
    )
    for month in months[:4]:
        name = calendar.month_name[int(month[5:7])]
        add("dates", f"invoices in {name} {month[:4]}", _invoice_month(month))

    add("types", "delivery notes", lambda t: t["document_type"] == "DELIVERY_NOTE")
    add("types", "purchase orders", lambda t: t["document_type"] == "PURCHASE_ORDER")

    items = sorted({line["description"] for t in truths.values() for line in t["line_items"]})
    for description in items[:: max(1, len(items) // 6)][:6]:
        add("text", description, _lists_item(description))
    return questions


async def _process(
    maker: async_sessionmaker[AsyncSession], settings: Any, storage: LocalStorage, root: Path
) -> tuple[User, dict[str, str], dict[str, Truth]]:
    manifest = generate_dataset(root, seed=DATASET_SEED, scenarios=SCENARIOS)
    async with maker() as session:
        await seed_demo_vendors(session)
        department = Department(name="Finance")
        session.add(department)
        await session.flush()
        user = User(
            email="analyst@eval.invalid",
            full_name="Evaluation analyst",
            password_hash="not-used",  # noqa: S106  (no login in the evaluation)
            role=Role.ANALYST,
            department_id=department.id,
        )
        session.add(user)
        await session.commit()
    ids: dict[str, str] = {}
    truths: dict[str, Truth] = {}
    for entry in manifest["documents"]:
        path = root / entry["file"]
        upload = UploadFile(
            io.BytesIO(path.read_bytes()),
            filename=path.name,
            headers=Headers({"content-type": entry["mime_type"]}),
        )
        async with maker() as session:
            document = await DocumentService(session, storage, settings).upload(
                actor=user,
                upload=upload,
                sensitivity=Sensitivity.INTERNAL,
                department_id=None,
                meta=SYSTEM_REQUEST,
            )
        ids[entry["doc_id"]] = str(document.id)
        truths[entry["doc_id"]] = json.loads(
            (root / entry["ground_truth"]).read_text(encoding="utf-8")
        )
    embedder = ChunkEmbedder(HashingEmbeddingProvider(), max_sensitivity=Sensitivity.INTERNAL)
    services = build_processing_services(settings, sessionmaker=maker, embedder=embedder)
    await Worker(
        settings=settings, sessionmaker=maker, storage=storage, services=services
    ).run_until_idle()
    return user, ids, truths


async def run_search_suite(output: Path, *, database_url: str, quick: bool = False) -> Report:
    started = time.perf_counter()
    embedder = ChunkEmbedder(HashingEmbeddingProvider(), max_sensitivity=Sensitivity.INTERNAL)
    rows: list[list[str]] = []
    per_question: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="docintel-search-eval-") as tmp:
        async with scratch_database(database_url) as url:
            engine = create_async_engine(url)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            try:
                settings = _settings(url, Path(tmp) / "storage")
                storage = LocalStorage(Path(tmp) / "storage")
                user, ids, truths = await _process(maker, settings, storage, Path(tmp) / "data")
                by_id = {value: key for key, value in ids.items()}
                questions = build_questions(truths)
                if quick:
                    questions = questions[::3]
                async with maker() as session:
                    service = DocumentSearchService(
                        session,
                        embedder,
                        min_similarity=settings.rag_min_dense_similarity,
                    )
                    for question in questions:
                        result = await service.search(
                            user,
                            question["question"],
                            limit=TEXT_DEPTH if question["family"] == "text" else 100,
                        )
                        returned = [by_id[str(hit.document.id)] for hit in result.hits]
                        per_question.append(
                            {
                                **question,
                                "returned": returned,
                                "recognized": list(result.parsed.recognized),
                                "free_text": result.parsed.text,
                                "mode": result.mode,
                            }
                        )
                    await session.rollback()
            finally:
                await engine.dispose()

    families: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    exact: dict[str, list[bool]] = defaultdict(list)
    failures: list[list[str]] = []
    for item in per_question:
        wanted: set[str] = set(item["relevant"])
        got: set[str] = set(item["returned"])
        counts = families[item["family"]]
        counts["tp"] += len(wanted & got)
        counts["fp"] += len(got - wanted)
        counts["fn"] += len(wanted - got)
        # Ranked text results: every listing document within the first TEXT_DEPTH.
        right = wanted <= got if item["family"] == "text" else wanted == got
        exact[item["family"]].append(right)
        if not right:
            failures.append(
                [
                    item["family"],
                    item["question"],
                    ", ".join(sorted(wanted - got)) or "-",
                    ", ".join(sorted(got - wanted)) or "-" if item["family"] != "text" else "n/a",
                ]
            )
    metrics: dict[str, Any] = {"families": {}, "questions": per_question}
    for family, counts in families.items():
        scores = counts_prf(counts["tp"], counts["fp"], counts["fn"])
        metrics["families"][family] = {
            **scores,
            "questions": len(exact[family]),
            "exact_set_rate": round(sum(exact[family]) / len(exact[family]), 4),
        }
        rows.append(
            [
                family,
                str(len(exact[family])),
                pct(scores["precision"]),
                pct(scores["recall"]),
                pct(scores["f1"]),
                pct(metrics["families"][family]["exact_set_rate"]),
            ]
        )
    report = Report(
        suite="search",
        title="Business document search evaluation",
        dataset={
            "generator_seed": DATASET_SEED,
            "scenarios": [scenario.value for scenario in SCENARIOS],
            "documents": len(truths),
            "questions": len(per_question),
        },
        config={
            "embedding_model": HASHING_MODEL,
            "text_depth": TEXT_DEPTH,
            "quick": quick,
            "seconds": round(time.perf_counter() - started, 1),
        },
        metrics=metrics,
        environment=environment({"embedding": HASHING_MODEL}),
        notes=[
            "Questions are generated from the ground truth of a synthetic dataset (native PDFs) "
            "and every document went through the production pipeline (classification, "
            "extraction, vendor resolution, indexing) first, so the scores measure parsing, "
            "extraction and filtering together.",
            "Structured families (vendor, payment terms, totals, dates, types) are scored as "
            "sets: precision and recall over all questions of the family, and the share of "
            "questions whose result set is exactly right.",
            f"Text questions return a ranked list (first {TEXT_DEPTH}); documents that list the "
            "item are relevant. Other documents sharing words with the item are returned too, "
            "so text precision is low by design.",
            "The first run of this suite found vendor names containing 'and' cut short "
            "('Harbor and Pine Packaging Ltd' read as 'Harbor'); the parser was fixed and the "
            "suite re-run. Questions come from the ground truth of clean native PDFs, so these "
            "structured-search scores are an upper bound: scanned documents are not included.",
            "Embeddings: lexical hashing model; semantic models not measured.",
        ],
        tables=[
            (
                "Results per question family",
                ["family", "questions", "precision", "recall", "F1", "exact (text: all found)"],
                rows,
            ),
            (
                "Questions with a different result set",
                ["family", "question", "missing", "unexpected"],
                failures or [["-", "none", "-", "-"]],
            ),
        ],
    )
    report.write(output)
    return report

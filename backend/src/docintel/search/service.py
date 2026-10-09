"""Search over business documents (Module 28): structured filters plus hybrid text search.

  request -> parse_query (types, vendor, payment terms, total, dates, free text)
          -> structured filters on the CURRENT extraction (corrections win), in SQL
          -> free text: full text + vectors over document_chunks of matching documents,
             fused (RRF) and grouped per document with the best passage as the snippet
          -> no free text: the matching documents, newest first

Access control is the same SQL predicate as every document read (visible_documents), applied
inside each query. Payment terms: documents whose terms were extracted are compared exactly;
for the others the indexed text is scanned for "Net 90" / "within 45 days" style terms, and
such matches say so in their reasons.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Numeric,
    String,
    Text,
    case,
    cast,
    func,
    literal,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.auth.policies import visible_documents
from docintel.db.models import (
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentType,
    ExtractedField,
    User,
    Vendor,
)
from docintel.fields.normalize import organization_key
from docintel.knowledge.embedding import ChunkEmbedder
from docintel.knowledge.retrieval import rrf_fuse, tsquery_literal
from docintel.matching.facts import NUMBER_FIELD, PO_REFERENCE_FIELD
from docintel.search.query import Comparison, ParsedQuery, parse_query

TOTAL_FIELDS = ("total", "contract_value")
DATE_FIELDS = ("invoice_date", "po_date", "delivery_date", "transaction_date", "effective_date")
VENDOR_FIELDS = ("vendor_name", "merchant_name")
TERMS_FIELD = "payment_terms_days"
NUMBER_FIELDS = tuple(dict.fromkeys(NUMBER_FIELD.values()))
_CANDIDATES = 60
_NUMBER = r"^-?[0-9]+(\.[0-9]+)?$"
_ISO_DATE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
_TERMS_IN_TEXT = re.compile(
    r"\b(?:net\s*(?P<net>\d{1,3})\b|(?:pay(?:ment)?\w*|due|terms?)[^.\n]{0,40}?"
    r"\b(?P<days>\d{1,3})\s*(?:calendar\s+)?days?\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Snippet:
    chunk_id: uuid.UUID
    text: str
    page_start: int | None
    page_end: int | None


@dataclass(slots=True)
class SearchHit:
    document: Document
    vendor_name: str | None
    document_date: date | None
    total: Decimal | None
    payment_terms_days: int | None
    score: float | None = None
    reasons: list[str] = field(default_factory=list)
    snippet: Snippet | None = None
    document_number: str | None = None
    order_reference: str | None = None  # the purchase order number a document quotes


@dataclass(slots=True)
class SearchResults:
    query: str
    parsed: ParsedQuery
    vendors: list[str]  # canonical vendor names the vendor phrase matched
    mode: str  # "structured", "text" or "structured+text"
    hits: list[SearchHit]
    total: int


# ------------------------------------------------------------------------------ SQL helpers
def _field_text(names: Sequence[str]) -> Any:
    """Normalized value (as text) of one of `names` in the document's current extraction; a
    reviewer's correction replaces the machine value."""
    source = case(
        (ExtractedField.corrected_value.is_not(None), ExtractedField.corrected_normalized),
        else_=ExtractedField.normalized_value,
    )
    return (
        select(source.op("->>", return_type=Text)(literal("value", String)))
        .join(DocumentExtraction, DocumentExtraction.id == ExtractedField.extraction_id)
        .where(
            DocumentExtraction.document_id == Document.id,
            DocumentExtraction.is_current.is_(True),
            ExtractedField.field_path.in_(names),
        )
        .order_by(ExtractedField.position)
        .limit(1)
        .scalar_subquery()
    )


def _as_number(value: Any) -> Any:
    return case((value.op("~")(_NUMBER), cast(value, Numeric)), else_=None)


def _as_date(value: Any) -> Any:
    return case((value.op("~")(_ISO_DATE), func.to_date(value, "YYYY-MM-DD")), else_=None)


def _compare(column: Any, comparison: Comparison) -> ColumnElement[bool]:
    value = comparison.value
    match comparison.op:
        case "gt":
            result: ColumnElement[bool] = column > value
        case "gte":
            result = column >= value
        case "lt":
            result = column < value
        case "lte":
            result = column <= value
        case _:
            result = column == value
    return result


def terms_in_text(content: str) -> list[tuple[int, str]]:
    """Payment terms written in a passage: (days, matched text)."""
    found = []
    for match in _TERMS_IN_TEXT.finditer(content):
        days = match["net"] or match["days"]
        if days is not None:
            found.append((int(days), " ".join(match.group(0).split())))
    return found


class DocumentSearchService:
    def __init__(
        self,
        session: AsyncSession,
        embedder: ChunkEmbedder | None,
        *,
        min_similarity: float = 0.5,
    ) -> None:
        self._session = session
        self._embedder = embedder
        # Vectors always have a "nearest" passage: only passages at least this similar count
        # as text matches (model-specific, like RAG_MIN_DENSE_SIMILARITY which it comes from).
        self._min_similarity = min_similarity

    async def _vendor_ids(self, name: str) -> tuple[list[uuid.UUID], list[str]]:
        key = organization_key(name)
        rows = (
            await self._session.execute(
                select(Vendor.id, Vendor.canonical_name).where(
                    or_(
                        Vendor.name_key == key,
                        Vendor.alias_keys.op("@>")(pg_array([key])),  # GIN index
                        Vendor.canonical_name.ilike(f"%{_escape(name)}%", escape="\\"),
                    )
                )
            )
        ).all()
        return [row.id for row in rows], [row.canonical_name for row in rows]

    async def search(
        self,
        user: User,
        query: str,
        *,
        limit: int = 20,
        document_types: Sequence[DocumentType] = (),
    ) -> SearchResults:
        parsed = parse_query(query)
        types = tuple(dict.fromkeys([*document_types, *parsed.document_types]))
        conditions: list[ColumnElement[bool]] = [visible_documents(user)]
        if types:
            conditions.append(Document.document_type.in_(types))

        vendor_names: list[str] = []
        if parsed.vendor:
            ids, vendor_names = await self._vendor_ids(parsed.vendor)
            printed = _field_text(VENDOR_FIELDS).ilike(f"%{_escape(parsed.vendor)}%", escape="\\")
            conditions.append(or_(Document.vendor_id.in_(ids), printed) if ids else printed)
        if parsed.total is not None:
            conditions.append(_compare(_as_number(_field_text(TOTAL_FIELDS)), parsed.total))
        document_date = _as_date(_field_text(DATE_FIELDS))
        if parsed.date_from is not None:
            conditions.append(document_date >= parsed.date_from)
        if parsed.date_to is not None:
            conditions.append(document_date <= parsed.date_to)

        text_terms: dict[uuid.UUID, tuple[int, str, DocumentChunk]] = {}
        if parsed.payment_terms_days is not None:
            terms = _as_number(_field_text([TERMS_FIELD]))
            extracted = _compare(terms, parsed.payment_terms_days)
            text_terms = await self._terms_from_text(conditions, terms, parsed.payment_terms_days)
            conditions.append(
                or_(extracted, Document.id.in_(list(text_terms))) if text_terms else extracted
            )

        if parsed.text:
            hits, total = await self._text_search(parsed.text, conditions, limit)
            mode = "structured+text" if parsed.has_filters else "text"
        else:
            hits, total = await self._structured(conditions, limit)
            mode = "structured"
        await self._describe(hits, parsed, text_terms)
        return SearchResults(query, parsed, vendor_names, mode, hits, total)

    async def _terms_from_text(
        self, conditions: list[ColumnElement[bool]], terms: Any, wanted: Comparison
    ) -> dict[uuid.UUID, tuple[int, str, DocumentChunk]]:
        """Documents without extracted payment terms whose text states matching terms."""
        rows = (
            await self._session.scalars(
                select(DocumentChunk)
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(
                    *conditions,
                    terms.is_(None),
                    DocumentChunk.content.op("~*")(r"(net\s*[0-9]|[0-9]\s*(calendar\s+)?days?)"),
                )
                .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
                .limit(500)
            )
        ).all()
        found: dict[uuid.UUID, tuple[int, str, DocumentChunk]] = {}
        for chunk in rows:
            if chunk.document_id in found:
                continue
            for days, quote in terms_in_text(chunk.content):
                if wanted.matches(Decimal(days)):
                    found[chunk.document_id] = (days, quote, chunk)
                    break
        return found

    async def _structured(
        self, conditions: list[ColumnElement[bool]], limit: int
    ) -> tuple[list[SearchHit], int]:
        total = await self._session.scalar(
            select(func.count()).select_from(Document).where(*conditions)
        )
        documents = (
            await self._session.scalars(
                select(Document)
                .where(*conditions)
                .order_by(
                    _as_date(_field_text(DATE_FIELDS)).desc().nulls_last(),
                    Document.created_at.desc(),
                    Document.id,
                )
                .limit(limit)
            )
        ).all()
        return [SearchHit(d, None, None, None, None) for d in documents], int(total or 0)

    async def _text_search(
        self, query: str, conditions: list[ColumnElement[bool]], limit: int
    ) -> tuple[list[SearchHit], int]:
        allowed = select(Document.id).where(*conditions)
        terms = list(
            await self._session.scalars(
                text("SELECT lexeme FROM unnest(to_tsvector('english', :q)) ORDER BY lexeme"),
                {"q": query},
            )
        )
        rankings: list[list[uuid.UUID]] = []
        if terms:
            tsquery = func.to_tsquery("simple", literal(" | ".join(map(tsquery_literal, terms))))
            rank = func.ts_rank_cd(DocumentChunk.search, tsquery, 32)
            lexical = await self._session.execute(
                select(DocumentChunk.id)
                .where(
                    DocumentChunk.document_id.in_(allowed),
                    DocumentChunk.search.op("@@")(tsquery),
                )
                .order_by(rank.desc(), DocumentChunk.content_hash)
                .limit(_CANDIDATES)
            )
            rankings.append([row.id for row in lexical])
        model = self._embedder.model if self._embedder is not None else None
        if self._embedder is not None and model is not None:
            vector = await self._embedder.embed_query(query)
            if vector is not None:
                distance = DocumentChunk.embedding.cosine_distance(vector)
                await self._session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
                dense = await self._session.execute(
                    select(DocumentChunk.id, distance.label("distance"))
                    .where(
                        DocumentChunk.document_id.in_(allowed),
                        DocumentChunk.embedding_model == model,
                        distance <= 1 - self._min_similarity,
                    )
                    .order_by(distance, DocumentChunk.content_hash)
                    .limit(_CANDIDATES)
                )
                rankings.insert(0, [row.id for row in dense])
        fused = rrf_fuse([ranking for ranking in rankings if ranking], k=60)
        if not fused:
            return [], 0
        chunks = {
            chunk.id: chunk
            for chunk in await self._session.scalars(
                select(DocumentChunk).where(DocumentChunk.id.in_([cid for cid, _ in fused]))
            )
        }
        best: dict[uuid.UUID, tuple[float, DocumentChunk]] = {}
        for chunk_id, score in fused:
            chunk = chunks.get(chunk_id)
            if chunk is not None and chunk.document_id not in best:
                best[chunk.document_id] = (score, chunk)
        ordered = sorted(best.items(), key=lambda item: -item[1][0])
        documents = {
            document.id: document
            for document in await self._session.scalars(
                select(Document).where(Document.id.in_([doc_id for doc_id, _ in ordered]))
            )
        }
        hits = []
        for document_id, (score, chunk) in ordered[:limit]:
            document = documents.get(document_id)
            if document is None:
                continue
            hits.append(
                SearchHit(
                    document,
                    None,
                    None,
                    None,
                    None,
                    score=round(score, 6),
                    snippet=Snippet(chunk.id, chunk.content, chunk.page_start, chunk.page_end),
                )
            )
        return hits, len(best)

    async def _describe(
        self,
        hits: list[SearchHit],
        parsed: ParsedQuery,
        text_terms: dict[uuid.UUID, tuple[int, str, DocumentChunk]],
    ) -> None:
        """Key facts of each hit (from its current extraction) and why it matched."""
        if not hits:
            return
        ids = [hit.document.id for hit in hits]
        rows = (
            await self._session.execute(
                select(
                    Document.id,
                    _field_text(VENDOR_FIELDS).label("vendor"),
                    _field_text(DATE_FIELDS).label("date"),
                    _field_text(TOTAL_FIELDS).label("total"),
                    _field_text([TERMS_FIELD]).label("terms"),
                    _field_text(NUMBER_FIELDS).label("number"),
                    _field_text([PO_REFERENCE_FIELD]).label("order"),
                    Vendor.canonical_name,
                )
                .outerjoin(Vendor, Vendor.id == Document.vendor_id)
                .where(Document.id.in_(ids))
            )
        ).all()
        facts = {row.id: row for row in rows}
        for hit in hits:
            row = facts.get(hit.document.id)
            if row is None:
                continue
            hit.vendor_name = row.canonical_name or row.vendor
            hit.document_date = _parse_date(row.date)
            hit.total = _parse_decimal(row.total)
            terms = _parse_decimal(row.terms)
            hit.payment_terms_days = int(terms) if terms is not None else None
            hit.document_number = row.number
            hit.order_reference = row.order
            reasons = []
            if hit.document.document_type is not None and parsed.document_types:
                reasons.append(f"type {hit.document.document_type.value}")
            if parsed.vendor:
                reasons.append(f"vendor {hit.vendor_name or parsed.vendor}")
            if parsed.payment_terms_days is not None:
                quoted = text_terms.get(hit.document.id)
                if quoted is not None and hit.payment_terms_days is None:
                    days, quote, chunk = quoted
                    page = f" (page {chunk.page_start})" if chunk.page_start else ""
                    reasons.append(f"payment terms {days} days read from the text: {quote!r}{page}")
                    hit.payment_terms_days = days
                    if hit.snippet is None:
                        hit.snippet = Snippet(
                            chunk.id, chunk.content, chunk.page_start, chunk.page_end
                        )
                elif hit.payment_terms_days is not None:
                    reasons.append(f"payment terms {hit.payment_terms_days} days (extracted)")
            if parsed.total is not None and hit.total is not None:
                reasons.append(f"total {hit.total:,}")
            if (parsed.date_from or parsed.date_to) and hit.document_date is not None:
                reasons.append(f"dated {hit.document_date.isoformat()}")
            if hit.snippet is not None and parsed.text:
                reasons.append(f"text matches {parsed.text!r}")
            hit.reasons = reasons


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _parse_decimal(value: str | None) -> Decimal | None:
    try:
        return Decimal(value) if value not in (None, "") else None
    except (InvalidOperation, TypeError):
        return None

"""Read access to structured extraction results and human field corrections.

Callers resolve the document through DocumentService.get first (access policy). A correction
re-runs the same consistency checks, confidence and routing as the worker (`score_fields`), so a
reviewer who fixes the uncertain fields moves the document out of review without reprocessing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.core.config import Settings
from docintel.core.errors import NotFoundError, UnprocessableContentError
from docintel.db.models import (
    AuditOutcome,
    Document,
    DocumentExtraction,
    DocumentStatus,
    ExtractedField,
    ReviewReason,
    User,
    Vendor,
)
from docintel.fields.schemas import schema_by_name
from docintel.fields.service import (
    apply_correction,
    context_from_signals,
    policy_from_settings,
    score_fields,
)
from docintel.fields.store import apply_scoring, resolved_from_row
from docintel.fields.vendors import match_in_session
from docintel.matching.service import MatchingService
from docintel.matching.store import lock_department

NO_EXTRACTION = "No structured extraction for this document."
FIELD_NOT_FOUND = "Field not found."
EXTRACTION_REASONS = frozenset(
    {
        ReviewReason.EXTRACTION_FAILED.value,
        ReviewReason.MISSING_REQUIRED_FIELDS.value,
        ReviewReason.EXTRACTION_UNCERTAIN.value,
        ReviewReason.EXTRACTION_INCONSISTENT.value,
    }
)


class ExtractionService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def current(self, document: Document) -> DocumentExtraction | None:
        extraction: DocumentExtraction | None = await self._session.scalar(
            select(DocumentExtraction).where(
                DocumentExtraction.document_id == document.id,
                DocumentExtraction.is_current.is_(True),
            )
        )
        return extraction

    async def require_current(self, document: Document) -> DocumentExtraction:
        extraction = await self.current(document)
        if extraction is None:
            raise NotFoundError(NO_EXTRACTION)
        return extraction

    async def vendor(self, document: Document) -> Vendor | None:
        if document.vendor_id is None:
            return None
        return await self._session.get(Vendor, document.vendor_id)

    async def correct_field(
        self,
        actor: User,
        document: Document,
        field_id: uuid.UUID,
        value: str,
        note: str | None,
        meta: RequestMeta,
    ) -> ExtractedField:
        # Same lock order as the worker's result transaction: department, then the document.
        await lock_department(self._session, document.department_id)
        locked = await self._session.scalar(
            select(Document)
            .where(Document.id == document.id)
            .with_for_update(of=Document)
            .execution_options(populate_existing=True)
        )
        if locked is None:
            raise NotFoundError("Document not found.")
        record = await self.require_current(locked)
        row = next((item for item in record.fields if item.id == field_id), None)
        if row is None:
            raise NotFoundError(FIELD_NOT_FOUND)
        schema = schema_by_name(record.schema_name)
        if schema is None:  # pragma: no cover - stored names come from the registry
            raise NotFoundError(NO_EXTRACTION)

        fields = [resolved_from_row(item) for item in record.fields]
        target = next(item for item in fields if item.path == row.field_path)
        context = context_from_signals(record.signals or {})
        if not apply_correction(target, value, context):
            msg = f"'{value.strip()}' is not a valid {target.value_type.value.lower()} value."
            raise UnprocessableContentError(msg)

        scalar = schema.scalar(target.name) if target.group is None else None
        if scalar is not None and scalar.meta.vendor and target.corrected_normalized is not None:
            match = (
                await match_in_session(
                    self._session,
                    target.corrected_value or None,
                    None,
                    min_score=self._settings.vendor_match_min_score,
                )
                if target.corrected_value
                else None
            )
            target.corrected_normalized["vendor"] = match.to_json() if match else None
            locked.vendor_id = match.vendor_id if match else None

        previous_level = record.review_level.value
        scoring = score_fields(schema, fields, policy_from_settings(self._settings))
        row.corrected_value = target.corrected_value
        row.corrected_normalized = target.corrected_normalized
        row.correction_note = note
        row.corrected_by_id = actor.id
        row.corrected_at = datetime.now(UTC)
        apply_scoring(record, schema, fields, scoring)

        reasons = [reason for reason in locked.review_reasons if reason not in EXTRACTION_REASONS]
        reasons += [reason.value for reason in scoring.reasons if reason.value not in reasons]
        locked.review_reasons = reasons
        previous_status = locked.status
        if locked.status in (DocumentStatus.REVIEW_REQUIRED, DocumentStatus.COMPLETED):
            # Corrected values change comparisons, duplicates and rules - here and in related
            # documents - and with them the review task and status.
            await MatchingService(self._session, self._settings).refresh(locked, actor=actor)

        # Values are document content: the audit trail records what changed, not the values.
        record_audit_event(
            self._session,
            action=AuditAction.DOCUMENT_FIELD_CORRECTED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="document",
            entity_id=locked.id,
            details={
                "extraction_id": str(record.id),
                "field_path": row.field_path,
                "value_type": row.value_type,
                "confirmed_empty": target.corrected_value == "",
                "note": bool(note),
                "review_level": {"from": previous_level, "to": scoring.level.value},
                "status": {"from": previous_status.value, "to": locked.status.value},
            },
        )
        await self._session.commit()
        await self._session.refresh(row)
        return row

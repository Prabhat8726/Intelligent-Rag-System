"""Rule administration: list, change parameters/severity/on-off (validated, versioned, audited)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.policies import visible_documents
from docintel.core.config import Settings
from docintel.core.errors import NotFoundError, UnprocessableContentError
from docintel.db.models import (
    AuditOutcome,
    BusinessRule,
    Document,
    RuleResultRecord,
    RuleSeverity,
    User,
)
from docintel.matching.service import rematch
from docintel.rules.engine import InvalidRuleParamsError, evaluator_for, validate_params

NOT_FOUND = "Rule not found."


def params_schema(rule_type: str) -> dict[str, Any]:
    evaluator = evaluator_for(rule_type)
    return evaluator.params_model.model_json_schema() if evaluator else {}


class RuleService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def all(self) -> list[BusinessRule]:
        return list(await self._session.scalars(select(BusinessRule).order_by(BusinessRule.code)))

    async def get(self, code: str) -> BusinessRule:
        rule = await self._session.scalar(select(BusinessRule).where(BusinessRule.code == code))
        if rule is None:
            raise NotFoundError(NOT_FOUND)
        return rule

    async def update(
        self,
        actor: User,
        code: str,
        *,
        params: dict[str, Any] | None,
        severity: RuleSeverity | None,
        is_enabled: bool | None,
        note: str | None,
        meta: RequestMeta,
    ) -> BusinessRule:
        rule = await self._session.scalar(
            select(BusinessRule).where(BusinessRule.code == code).with_for_update(of=BusinessRule)
        )
        if rule is None:
            raise NotFoundError(NOT_FOUND)
        before = {
            "params": rule.params,
            "severity": rule.severity.value,
            "enabled": rule.is_enabled,
        }
        if params is not None:
            try:
                rule.params = validate_params(rule.rule_type, params)
            except InvalidRuleParamsError as exc:
                raise UnprocessableContentError(f"Invalid parameters: {exc}") from exc
        if severity is not None:
            rule.severity = severity
        if is_enabled is not None:
            rule.is_enabled = is_enabled
        after = {"params": rule.params, "severity": rule.severity.value, "enabled": rule.is_enabled}
        if after == before:
            return rule
        rule.version += 1
        rule.updated_by_id = actor.id
        # Rule configuration is not document content: the audit trail keeps both versions.
        record_audit_event(
            self._session,
            action=AuditAction.RULE_UPDATED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="business_rule",
            entity_id=rule.id,
            details={
                "code": rule.code,
                "version": rule.version,
                "before": before,
                "after": after,
                "note": note,
            },
        )
        await self._session.commit()
        await self._session.refresh(rule)
        return rule

    async def evaluate(
        self, actor: User, document_ids: list[uuid.UUID], meta: RequestMeta
    ) -> list[tuple[uuid.UUID, bool, list[RuleResultRecord]]]:
        """Re-run matching and rules for documents the actor can see (e.g. after a rule change).

        One transaction per document, so the department locks are never held two at a time.
        """
        visible = set(
            await self._session.scalars(
                select(Document.id).where(Document.id.in_(document_ids), visible_documents(actor))
            )
        )
        missing = [str(document_id) for document_id in document_ids if document_id not in visible]
        if missing:
            raise NotFoundError(f"Documents not found: {', '.join(missing[:5])}.")
        outcome: list[tuple[uuid.UUID, bool, list[RuleResultRecord]]] = []
        for document_id in dict.fromkeys(document_ids):
            done = await rematch(self._session, self._settings, document_id, actor=actor)
            await self._session.commit()
            results = list(
                await self._session.scalars(
                    select(RuleResultRecord)
                    .where(RuleResultRecord.document_id == document_id)
                    .order_by(RuleResultRecord.rule_code)
                )
            )
            outcome.append((document_id, done, results))
        record_audit_event(
            self._session,
            action=AuditAction.RULES_EVALUATED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="document",
            entity_id=document_ids[0],
            details={"documents": [str(document_id) for document_id in document_ids]},
        )
        await self._session.commit()
        return outcome

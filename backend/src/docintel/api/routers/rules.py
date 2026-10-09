"""Business rule endpoints (Module 10): read the configuration, change it, re-evaluate."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from docintel.api.deps import RequestMetaDep, SessionDep, SettingsDep, require_permission
from docintel.api.schemas.common import PROBLEM_RESPONSES, ProblemDetail
from docintel.api.schemas.matching import (
    RuleEvaluate,
    RuleEvaluation,
    RuleRead,
    RuleResultRead,
    RuleUpdate,
)
from docintel.auth.permissions import Permission
from docintel.db.models import BusinessRule, User
from docintel.rules.service import RuleService, params_schema

router = APIRouter(
    prefix="/rules",
    tags=["rules"],
    responses={
        **PROBLEM_RESPONSES,
        404: {"model": ProblemDetail, "description": "Not found or not accessible"},
    },
)
Reader = Annotated[User, Depends(require_permission(Permission.RULES_READ))]
Manager = Annotated[User, Depends(require_permission(Permission.RULES_MANAGE))]
Processor = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_PROCESS))]


def _read(rule: BusinessRule) -> RuleRead:
    data = RuleRead.model_validate(rule)
    data.params_schema = params_schema(rule.rule_type)
    return data


@router.get("", response_model=list[RuleRead], summary="Every configured business rule")
async def list_rules(user: Reader, session: SessionDep, settings: SettingsDep) -> list[RuleRead]:
    return [_read(rule) for rule in await RuleService(session, settings).all()]


@router.get("/{code}", response_model=RuleRead, summary="One rule, with its parameter schema")
async def get_rule(code: str, user: Reader, session: SessionDep, settings: SettingsDep) -> RuleRead:
    return _read(await RuleService(session, settings).get(code))


@router.patch(
    "/{code}",
    response_model=RuleRead,
    summary="Change a rule's parameters, severity or on/off (validated, versioned, audited)",
    description="Applies to evaluations from now on; use POST /rules/evaluate to re-evaluate "
    "documents already processed.",
)
async def update_rule(
    code: str,
    body: RuleUpdate,
    user: Manager,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> RuleRead:
    rule = await RuleService(session, settings).update(
        user,
        code,
        params=body.params,
        severity=body.severity,
        is_enabled=body.is_enabled,
        note=body.note,
        meta=meta,
    )
    return _read(rule)


@router.post(
    "/evaluate",
    response_model=list[RuleEvaluation],
    summary="Re-run matching and the rules for documents (e.g. after changing a rule)",
    description="Comparisons, duplicates, rule results and review tasks are rebuilt for each "
    "document and the documents related to it.",
)
async def evaluate_rules(
    body: RuleEvaluate,
    user: Processor,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> list[RuleEvaluation]:
    outcome = await RuleService(session, settings).evaluate(user, body.document_ids, meta)
    return [
        RuleEvaluation(
            document_id=document_id,
            evaluated=evaluated,
            results=[RuleResultRead.model_validate(result) for result in results],
        )
        for document_id, evaluated, results in outcome
    ]

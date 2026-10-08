"""Role → permission map. The single source of truth for RBAC (see docs/architecture/05).

Roles are a closed, code-reviewed set (ADR-006): changing what a role may do is a code change
with tests, never a data edit.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType

from docintel.db.models import Role


class Permission(StrEnum):
    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_UPLOAD = "documents:upload"
    DOCUMENTS_PROCESS = "documents:process"
    DOCUMENTS_REVIEW = "documents:review"
    DOCUMENTS_DELETE = "documents:delete"
    COMPARISONS_CREATE = "comparisons:create"
    RULES_READ = "rules:read"
    RULES_MANAGE = "rules:manage"
    REVIEWS_WORK = "reviews:work"
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_MANAGE = "knowledge:manage"
    ANALYSIS_RUN = "analysis:run"
    ANALYSIS_READ = "analysis:read"
    WORKFLOWS_START = "workflows:start"
    WORKFLOWS_READ = "workflows:read"
    WORKFLOWS_APPROVE = "workflows:approve"
    REPORTS_CREATE = "reports:create"
    REPORTS_READ = "reports:read"
    AUDIT_READ = "audit:read"
    DASHBOARD_READ = "dashboard:read"
    EVALUATIONS_READ = "evaluations:read"
    VENDORS_MANAGE = "vendors:manage"
    USERS_MANAGE = "users:manage"


P = Permission

_VIEWER = frozenset(
    {
        P.DOCUMENTS_READ,
        P.RULES_READ,
        P.KNOWLEDGE_READ,
        P.ANALYSIS_READ,
        P.WORKFLOWS_READ,
        P.REPORTS_READ,
        P.DASHBOARD_READ,
    }
)

_REVIEWER = _VIEWER | {
    P.DOCUMENTS_REVIEW,
    P.COMPARISONS_CREATE,
    P.REVIEWS_WORK,
    P.ANALYSIS_RUN,
    P.WORKFLOWS_APPROVE,
    P.REPORTS_CREATE,
}

_ANALYST = _VIEWER | {
    P.DOCUMENTS_UPLOAD,
    P.DOCUMENTS_PROCESS,
    P.DOCUMENTS_REVIEW,
    P.COMPARISONS_CREATE,
    P.REVIEWS_WORK,
    P.ANALYSIS_RUN,
    P.WORKFLOWS_START,
    P.REPORTS_CREATE,
    P.EVALUATIONS_READ,
}

_MANAGER = (
    _ANALYST
    | _REVIEWER
    | {
        P.DOCUMENTS_DELETE,
        P.KNOWLEDGE_MANAGE,
        P.AUDIT_READ,
        P.VENDORS_MANAGE,
    }
)

ROLE_PERMISSIONS: MappingProxyType[Role, frozenset[Permission]] = MappingProxyType(
    {
        Role.VIEWER: frozenset(_VIEWER),
        Role.REVIEWER: frozenset(_REVIEWER),
        Role.ANALYST: frozenset(_ANALYST),
        Role.MANAGER: frozenset(_MANAGER),
        Role.ADMIN: frozenset(Permission),
    }
)


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[role]


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]

"""Locks the role/permission matrix documented in docs/architecture/05-api-specification.md."""

from __future__ import annotations

import pytest

from docintel.auth.permissions import ROLE_PERMISSIONS, Permission, has_permission
from docintel.db.models import Role

A, M, AN, R, V = Role.ADMIN, Role.MANAGER, Role.ANALYST, Role.REVIEWER, Role.VIEWER

EXPECTED: dict[Permission, set[Role]] = {
    Permission.DOCUMENTS_READ: {A, M, AN, R, V},
    Permission.DOCUMENTS_UPLOAD: {A, M, AN},
    Permission.DOCUMENTS_PROCESS: {A, M, AN},
    Permission.DOCUMENTS_REVIEW: {A, M, AN, R},
    Permission.DOCUMENTS_DELETE: {A, M},
    Permission.COMPARISONS_CREATE: {A, M, AN, R},
    Permission.RULES_READ: {A, M, AN, R, V},
    Permission.RULES_MANAGE: {A},
    Permission.REVIEWS_WORK: {A, M, AN, R},
    Permission.KNOWLEDGE_READ: {A, M, AN, R, V},
    Permission.KNOWLEDGE_MANAGE: {A, M},
    Permission.ANALYSIS_RUN: {A, M, AN, R},
    Permission.ANALYSIS_READ: {A, M, AN, R, V},
    Permission.WORKFLOWS_START: {A, M, AN},
    Permission.WORKFLOWS_READ: {A, M, AN, R, V},
    Permission.WORKFLOWS_APPROVE: {A, M, R},
    Permission.REPORTS_CREATE: {A, M, AN, R},
    Permission.REPORTS_READ: {A, M, AN, R, V},
    Permission.AUDIT_READ: {A, M},
    Permission.DASHBOARD_READ: {A, M, AN, R, V},
    Permission.EVALUATIONS_READ: {A, M, AN},
    Permission.USERS_MANAGE: {A},
}


def test_every_permission_is_specified() -> None:
    assert set(EXPECTED) == set(Permission)


def test_every_role_is_mapped() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)


@pytest.mark.parametrize("permission", list(Permission))
def test_matrix_matches_documentation(permission: Permission) -> None:
    granted = {role for role in Role if has_permission(role, permission)}
    assert granted == EXPECTED[permission]


def test_analyst_cannot_approve_and_viewer_is_read_only() -> None:
    assert not has_permission(Role.ANALYST, Permission.WORKFLOWS_APPROVE)
    viewer_permissions = ROLE_PERMISSIONS[Role.VIEWER]
    assert all(p.value.endswith(":read") for p in viewer_permissions)

"""Resource-level access policy (department scoping on top of RBAC).

Rules (docs/architecture/09-security-architecture.md):
* ADMIN sees every non-deleted document.
* Everyone else sees documents of their own department plus documents they own.
* Soft-deleted documents are invisible to every read path.
* Knowledge documents are organization-wide (no department) or restricted to one department;
  only that department (and ADMIN) can read a restricted one - including its chunks in RAG.

The policy is expressed as SQL predicates so it is applied inside queries (lists, search,
duplicate lookup, later RAG and agent tools) - never by filtering results in Python.
Inaccessible resources are reported as "not found" to avoid confirming that they exist.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, false, or_, true

from docintel.db.models import Document, KnowledgeChunk, KnowledgeDocument, Role, User


def visible_documents(user: User) -> ColumnElement[bool]:
    not_deleted = Document.deleted_at.is_(None)
    if user.role == Role.ADMIN:
        return not_deleted
    scope: ColumnElement[bool] = Document.owner_id == user.id
    if user.department_id is not None:
        scope = or_(scope, Document.department_id == user.department_id)
    return and_(not_deleted, scope)


def can_view_document(user: User, document: Document) -> bool:
    """In-memory twin of `visible_documents` for objects that are already loaded."""
    if document.deleted_at is not None:
        return False
    if user.role == Role.ADMIN:
        return True
    return document.owner_id == user.id or (
        user.department_id is not None and document.department_id == user.department_id
    )


def no_documents() -> ColumnElement[bool]:
    return false()


def visible_knowledge(user: User) -> ColumnElement[bool]:
    not_deleted = KnowledgeDocument.deleted_at.is_(None)
    if user.role == Role.ADMIN:
        return not_deleted
    scope: ColumnElement[bool] = KnowledgeDocument.department_id.is_(None)
    if user.department_id is not None:
        scope = or_(scope, KnowledgeDocument.department_id == user.department_id)
    return and_(not_deleted, scope)


def visible_knowledge_chunks(user: User) -> ColumnElement[bool]:
    """Chunks carry their document's department, so retrieval filters in the index scan."""
    if user.role == Role.ADMIN:
        return true()
    scope: ColumnElement[bool] = KnowledgeChunk.department_id.is_(None)
    if user.department_id is not None:
        scope = or_(scope, KnowledgeChunk.department_id == user.department_id)
    return scope

"""ORM models. Importing this package registers every table on `Base.metadata`."""

from docintel.db.models.audit import ActorType, AuditLog, AuditOutcome
from docintel.db.models.documents import (
    Document,
    DocumentSource,
    DocumentStatus,
    DocumentType,
    DocumentVersion,
    JobStatus,
    JobType,
    ProcessingJob,
    Sensitivity,
)
from docintel.db.models.identity import Department, Role, User

__all__ = [
    "ActorType",
    "AuditLog",
    "AuditOutcome",
    "Department",
    "Document",
    "DocumentSource",
    "DocumentStatus",
    "DocumentType",
    "DocumentVersion",
    "JobStatus",
    "JobType",
    "ProcessingJob",
    "Role",
    "Sensitivity",
    "User",
]

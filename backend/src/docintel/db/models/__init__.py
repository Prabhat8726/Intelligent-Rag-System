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
from docintel.db.models.understanding import (
    ClassificationMethod,
    DocumentClassification,
    DocumentPage,
    DocumentTable,
    ExtractionMethod,
    ReviewReason,
    TableMethod,
    TableRow,
)

__all__ = [
    "ActorType",
    "AuditLog",
    "AuditOutcome",
    "ClassificationMethod",
    "Department",
    "Document",
    "DocumentClassification",
    "DocumentPage",
    "DocumentSource",
    "DocumentStatus",
    "DocumentTable",
    "DocumentType",
    "DocumentVersion",
    "ExtractionMethod",
    "JobStatus",
    "JobType",
    "ProcessingJob",
    "ReviewReason",
    "Role",
    "Sensitivity",
    "TableMethod",
    "TableRow",
    "User",
]

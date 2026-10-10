"""Code-defined workflows: which document type they take, their steps and the request the
investigation step answers. Changing a definition is a code change with a new version number;
each workflow instance records the version it ran with."""

from __future__ import annotations

from dataclasses import dataclass

from docintel.db.models import DocumentType, ReportType, WorkflowType

# Step names (stored in workflow_tasks.step_name).
CHECK_DOCUMENT = "check_document"
COMPARE_VERSIONS = "compare_versions"
INVESTIGATE = "investigate"
PROPOSE_ACTION = "propose_action"
APPROVAL = "approval"
EXECUTE_ACTION = "execute_action"
REPORT = "report"


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    workflow_type: WorkflowType
    version: int
    title: str
    document_type: DocumentType
    steps: tuple[str, ...]
    query: str  # what the investigation step is asked (as the person who started it)
    report_type: ReportType


DEFINITIONS: dict[WorkflowType, WorkflowDefinition] = {
    WorkflowType.INVOICE_PROCESSING: WorkflowDefinition(
        workflow_type=WorkflowType.INVOICE_PROCESSING,
        version=1,
        title="Invoice processing",
        document_type=DocumentType.INVOICE,
        steps=(CHECK_DOCUMENT, INVESTIGATE, PROPOSE_ACTION, APPROVAL, EXECUTE_ACTION, REPORT),
        query="Can we pay this invoice?",
        report_type=ReportType.INVOICE_VERIFICATION,
    ),
    WorkflowType.CONTRACT_REVIEW: WorkflowDefinition(
        workflow_type=WorkflowType.CONTRACT_REVIEW,
        version=1,
        title="Contract review",
        document_type=DocumentType.CONTRACT,
        steps=(
            CHECK_DOCUMENT,
            COMPARE_VERSIONS,
            INVESTIGATE,
            PROPOSE_ACTION,
            APPROVAL,
            EXECUTE_ACTION,
            REPORT,
        ),
        query="Does this contract follow our contract guidelines?",
        report_type=ReportType.CONTRACT_REVIEW,
    ),
}


def definition_for(workflow_type: WorkflowType) -> WorkflowDefinition:
    return DEFINITIONS[workflow_type]


def definition_by_document_type(document_type: DocumentType) -> WorkflowDefinition | None:
    return next(
        (item for item in DEFINITIONS.values() if item.document_type == document_type), None
    )

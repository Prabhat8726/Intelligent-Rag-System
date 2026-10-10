"""Workflows: instances, steps, human-approved actions with their transitions, reports;
workflow jobs; contract policy rules.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-10
"""

import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKFLOW_TYPES = ("INVOICE_PROCESSING", "CONTRACT_REVIEW")
WORKFLOW_STATUSES = (
    "QUEUED",
    "RUNNING",
    "AWAITING_APPROVAL",
    "COMPLETED",
    "REJECTED",
    "FAILED",
    "CANCELLED",
)
TRIGGERS = ("MANUAL", "AUTO")
STEP_STATUSES = ("PENDING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED")
ACTION_TYPES = (
    "APPROVE_FOR_PAYMENT",
    "REJECT_DUPLICATE",
    "REQUEST_VENDOR_CLARIFICATION",
    "HOLD_FOR_REVIEW",
    "APPROVE_CONTRACT",
    "REQUEST_LEGAL_REVIEW",
)
ACTION_STATUSES = ("PROPOSED", "AWAITING_APPROVAL", "APPROVED", "REJECTED", "EXECUTED", "FAILED")
RISKS = ("LOW", "MEDIUM", "HIGH")
PROPOSERS = ("RULES", "AGENT", "USER")
ACTOR_TYPES = ("USER", "SYSTEM", "AGENT", "WORKER", "ANONYMOUS")
REPORT_TYPES = (
    "INVOICE_VERIFICATION",
    "CONTRACT_REVIEW",
    "DOCUMENT_COMPARISON",
    "COMPLIANCE_REVIEW",
    "AI_ANALYSIS",
)
REPORT_SUBJECTS = ("DOCUMENT", "COMPARISON", "AGENT_RUN")
JOB_TYPES_BEFORE = ("DOCUMENT_PROCESSING", "KNOWLEDGE_PROCESSING", "AGENT_ANALYSIS")
JOB_TYPES = (*JOB_TYPES_BEFORE, "WORKFLOW")

# Frozen copy of the contract rules added to docintel.rules.defaults at this revision.
CONTRACT_RULES: list[dict[str, Any]] = [
    {
        "code": "CONTRACT_REQUIRED_CLAUSES",
        "rule_type": "required_clauses",
        "name": "Mandatory contract clauses missing",
        "description": "Every supplier contract contains term and termination, limitation of "
        "liability and governing law clauses (Contract Management Guidelines, section 2).",
        "applies_to": ["CONTRACT"],
        "params": {"clauses": ["Termination", "Liability", "Governing Law"]},
        "severity": "HIGH",
    },
    {
        "code": "CONTRACT_TERMINATION_NOTICE",
        "rule_type": "clause_notice_period",
        "name": "Termination notice too long",
        "description": "The company may terminate for convenience with no more than 90 days "
        "notice (Contract Management Guidelines 2.1).",
        "applies_to": ["CONTRACT"],
        "params": {"clause": "Termination", "max_days": 90},
        "severity": "MEDIUM",
    },
    {
        "code": "CONTRACT_GOVERNING_LAW",
        "rule_type": "governing_law",
        "name": "Governing law needs Legal's approval",
        "description": "Contracts are governed by the law of the State of Ohio unless Legal "
        "approves another jurisdiction (Contract Management Guidelines 2.4).",
        "applies_to": ["CONTRACT"],
        "params": {"clause": "Governing Law", "allowed": ["Ohio"]},
        "severity": "MEDIUM",
    },
]


def _enum(values: tuple[str, ...], name: str, length: int = 20) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _clock(name: str) -> sa.Column[datetime]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("clock_timestamp()"),
        nullable=False,
    )


def _fk(column: str, table: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"{target}.id"],
        name=op.f(f"fk_{table}_{column}_{target}"),
        ondelete=ondelete,
    )


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_type", _enum(WORKFLOW_TYPES, "workflow_type", 40), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=False),
        sa.Column("status", _enum(WORKFLOW_STATUSES, "status"), nullable=False),
        sa.Column("trigger", _enum(TRIGGERS, "trigger"), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("initiated_by_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("current_step", sa.String(length=40), nullable=True),
        sa.Column("outcome", sa.String(length=60), nullable=True),
        sa.Column("error", sa.String(length=1000), nullable=True),
        _clock("created_at"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _clock("updated_at"),
        sa.CheckConstraint(
            "(finished_at IS NOT NULL) = "
            "(status IN ('COMPLETED', 'REJECTED', 'FAILED', 'CANCELLED'))",
            name=op.f("ck_workflows_finished_when_done"),
        ),
        sa.CheckConstraint(
            "definition_version >= 1", name=op.f("ck_workflows_definition_version_positive")
        ),
        _fk("document_id", "workflows", "documents", "CASCADE"),
        _fk("document_version_id", "workflows", "document_versions", "CASCADE"),
        _fk("initiated_by_id", "workflows", "users", "RESTRICT"),
        _fk("agent_run_id", "workflows", "agent_runs", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflows")),
    )
    op.create_index("ix_workflows_document_id", "workflows", ["document_id"])
    op.create_index("ix_workflows_status", "workflows", ["status"])
    op.create_index(
        "ix_workflows_initiated_by_created", "workflows", ["initiated_by_id", "created_at"]
    )
    op.create_index(
        "uq_workflows_active_document_type",
        "workflows",
        ["document_id", "workflow_type"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING', 'AWAITING_APPROVAL')"),
    )

    op.create_table(
        "workflow_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.String(length=40), nullable=False),
        sa.Column("status", _enum(STEP_STATUSES, "status"), nullable=False),
        sa.Column("output", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_workflow_tasks_sequence_positive")),
        _fk("workflow_id", "workflow_tasks", "workflows", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_tasks")),
        sa.UniqueConstraint("workflow_id", "sequence", name=op.f("uq_workflow_tasks_workflow_id")),
    )

    op.create_table(
        "workflow_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", _enum(ACTION_TYPES, "action_type", 40), nullable=False),
        sa.Column("status", _enum(ACTION_STATUSES, "status"), nullable=False),
        sa.Column("risk_level", _enum(RISKS, "risk_level"), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("required_role", sa.String(length=20), nullable=True),
        sa.Column("proposed_by_type", _enum(PROPOSERS, "proposed_by"), nullable=False),
        sa.Column("proposed_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("confidence_level", sa.String(length=10), nullable=True),
        sa.Column("confidence_score", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("payload", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("maker_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("decided_by_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_result", _jsonb(), nullable=True),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        _clock("created_at"),
        _clock("updated_at"),
        sa.CheckConstraint(
            "decided_by_id IS NULL OR NOT (decided_by_id = ANY (maker_ids))",
            name=op.f("ck_workflow_actions_maker_checker"),
        ),
        sa.CheckConstraint(
            "status <> 'REJECTED' OR char_length(btrim(coalesce(decision_reason, ''))) > 0",
            name=op.f("ck_workflow_actions_rejection_has_reason"),
        ),
        sa.CheckConstraint(
            "NOT requires_approval OR required_role IS NOT NULL",
            name=op.f("ck_workflow_actions_approval_has_role"),
        ),
        sa.CheckConstraint(
            "NOT requires_approval OR status IN ('PROPOSED', 'AWAITING_APPROVAL') "
            "OR decided_by_id IS NOT NULL",
            name=op.f("ck_workflow_actions_approved_by_person"),
        ),
        sa.CheckConstraint(
            "status IN ('PROPOSED', 'AWAITING_APPROVAL') OR decided_at IS NOT NULL",
            name=op.f("ck_workflow_actions_decided_at_when_decided"),
        ),
        sa.CheckConstraint(
            "status <> 'EXECUTED' OR executed_at IS NOT NULL",
            name=op.f("ck_workflow_actions_executed_at_set"),
        ),
        sa.CheckConstraint(
            "char_length(rationale) <= 2000", name=op.f("ck_workflow_actions_rationale_length")
        ),
        sa.CheckConstraint(
            "decision_reason IS NULL OR char_length(decision_reason) <= 1000",
            name=op.f("ck_workflow_actions_decision_reason_length"),
        ),
        sa.CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)",
            name=op.f("ck_workflow_actions_confidence_score_range"),
        ),
        _fk("workflow_id", "workflow_actions", "workflows", "CASCADE"),
        _fk("document_id", "workflow_actions", "documents", "CASCADE"),
        _fk("document_version_id", "workflow_actions", "document_versions", "CASCADE"),
        _fk("proposed_by_user_id", "workflow_actions", "users", "RESTRICT"),
        _fk("agent_run_id", "workflow_actions", "agent_runs", "SET NULL"),
        _fk("decided_by_id", "workflow_actions", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_actions")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_workflow_actions_idempotency_key")),
    )
    op.create_index("ix_workflow_actions_workflow_id", "workflow_actions", ["workflow_id"])
    op.create_index("ix_workflow_actions_document_id", "workflow_actions", ["document_id"])
    op.create_index("ix_workflow_actions_status", "workflow_actions", ["status"])

    op.create_table(
        "workflow_action_transitions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", _enum(ACTION_STATUSES, "from_status"), nullable=True),
        sa.Column("to_status", _enum(ACTION_STATUSES, "to_status"), nullable=False),
        sa.Column("actor_type", _enum(ACTOR_TYPES, "actor_type"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        _clock("created_at"),
        sa.CheckConstraint(
            "reason IS NULL OR char_length(reason) <= 1000",
            name=op.f("ck_workflow_action_transitions_reason_length"),
        ),
        _fk("action_id", "workflow_action_transitions", "workflow_actions", "CASCADE"),
        _fk("actor_id", "workflow_action_transitions", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_action_transitions")),
    )
    op.create_index(
        "ix_workflow_action_transitions_action_id",
        "workflow_action_transitions",
        ["action_id", "id"],
    )
    # History is never rewritten (deleting with the document stays possible).
    op.execute(
        """
        CREATE FUNCTION workflow_action_transitions_reject_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'workflow_action_transitions is append-only: % is not permitted',
                TG_OP;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER workflow_action_transitions_no_update
        BEFORE UPDATE ON workflow_action_transitions
        FOR EACH ROW EXECUTE FUNCTION workflow_action_transitions_reject_update()
        """
    )

    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_type", _enum(REPORT_TYPES, "report_type", 40), nullable=False),
        sa.Column("subject_type", _enum(REPORT_SUBJECTS, "subject_type"), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("document_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("snapshot", _jsonb(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_by_id", sa.Uuid(), nullable=False),
        _clock("created_at"),
        sa.CheckConstraint("cardinality(document_ids) >= 1", name=op.f("ck_reports_has_documents")),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_reports_sha256_hex")),
        sa.CheckConstraint(
            "template_version >= 1", name=op.f("ck_reports_template_version_positive")
        ),
        _fk("workflow_id", "reports", "workflows", "SET NULL"),
        _fk("generated_by_id", "reports", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
    )
    op.create_index("ix_reports_subject", "reports", ["subject_type", "subject_id"])
    op.create_index("ix_reports_generated_by_created", "reports", ["generated_by_id", "created_at"])
    op.create_index("ix_reports_workflow_id", "reports", ["workflow_id"])
    op.create_index("ix_reports_document_ids", "reports", ["document_ids"], postgresql_using="gin")

    # Workflows run in the worker through the same job queue.
    op.add_column("processing_jobs", sa.Column("workflow_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_processing_jobs_workflow_id_workflows"),
        "processing_jobs",
        "workflows",
        ["workflow_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_processing_jobs_workflow_id", "processing_jobs", ["workflow_id"])
    _replace_check("processing_jobs", "job_type", JOB_TYPES)

    # Contract policy rules (an existing rule with the same code is left alone).
    connection = op.get_bind()
    for rule in CONTRACT_RULES:
        connection.execute(
            sa.text(
                "INSERT INTO business_rules (id, code, rule_type, name, description, applies_to, "
                "params, severity, is_enabled, version) VALUES (:id, :code, :rule_type, :name, "
                ":description, :applies_to, CAST(:params AS jsonb), :severity, true, 1) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {
                **rule,
                "id": uuid.uuid5(uuid.NAMESPACE_URL, f"docintel:rule:{rule['code']}"),
                "params": json.dumps(rule["params"]),
            },
        )


def _replace_check(table: str, column: str, values: tuple[str, ...]) -> None:
    name = op.f(f"ck_{table}_{column}")
    op.drop_constraint(name, table, type_="check")
    allowed = ", ".join(f"'{value}'" for value in values)
    op.create_check_constraint(name, table, f"{column} IN ({allowed})")


def downgrade() -> None:
    codes = [rule["code"] for rule in CONTRACT_RULES]
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM rule_results WHERE rule_code = ANY(:codes)"), {"codes": codes}
    )
    connection.execute(
        sa.text("DELETE FROM business_rules WHERE code = ANY(:codes)"), {"codes": codes}
    )
    op.execute("DELETE FROM processing_jobs WHERE job_type = 'WORKFLOW'")
    _replace_check("processing_jobs", "job_type", JOB_TYPES_BEFORE)
    op.drop_index("ix_processing_jobs_workflow_id", table_name="processing_jobs")
    op.drop_constraint(
        op.f("fk_processing_jobs_workflow_id_workflows"), "processing_jobs", type_="foreignkey"
    )
    op.drop_column("processing_jobs", "workflow_id")
    op.drop_table("reports")
    op.execute(
        "DROP TRIGGER IF EXISTS workflow_action_transitions_no_update "
        "ON workflow_action_transitions"
    )
    op.execute("DROP FUNCTION IF EXISTS workflow_action_transitions_reject_update()")
    op.drop_table("workflow_action_transitions")
    op.drop_table("workflow_actions")
    op.drop_table("workflow_tasks")
    op.drop_table("workflows")

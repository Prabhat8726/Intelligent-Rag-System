"""Comparisons, business rules, rule results, the review queue and document key facts.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-09
"""

import json
import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMPARISON_TYPES = ("INVOICE_PO", "INVOICE_DELIVERY", "INVOICE_PO_DELIVERY", "PO_DELIVERY")
ORIGINS = ("AUTO", "MANUAL")
ROLES = ("INVOICE", "PURCHASE_ORDER", "DELIVERY_NOTE")
ITEM_STATUSES = ("MATCH", "MISMATCH", "MISSING", "UNCERTAIN")
CATEGORIES = ("HEADER", "LINE_ITEM")
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
OUTCOMES = ("PASS", "FAIL", "WARN", "ERROR", "NOT_APPLICABLE")
TASK_TYPES = (
    "DUPLICATE_REVIEW",
    "DISCREPANCY_REVIEW",
    "EXTRACTION_REVIEW",
    "CLASSIFICATION_REVIEW",
)
TASK_STATUSES = ("OPEN", "IN_PROGRESS", "RESOLVED", "CANCELLED")
PRIORITIES = ("URGENT", "HIGH", "NORMAL", "LOW")
RESOLUTIONS = ("APPROVED", "CORRECTED", "REJECTED", "CLEARED")

# Frozen copy of docintel.rules.defaults.DEFAULT_RULES at this revision (parameters validated).
DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "code": "INV_DUPLICATE",
        "rule_type": "duplicate_document",
        "name": "Duplicate invoice",
        "description": (
            "An earlier invoice has the same vendor and number (fail), or the same vendor, "
            "amount and a date within the window (warn)."
        ),
        "applies_to": ["INVOICE"],
        "severity": "HIGH",
        "params": {"date_window_days": 7, "match_amount_and_date": True},
    },
    {
        "code": "INV_PO_UNIT_PRICE",
        "rule_type": "line_unit_price",
        "name": "Unit price differs from the purchase order",
        "description": (
            "Invoice unit prices must equal the purchase order's within the tolerance."
        ),
        "applies_to": ["INVOICE"],
        "severity": "HIGH",
        "params": {"tolerance_pct": "0", "tolerance_abs": "0.01"},
    },
    {
        "code": "INV_PO_QUANTITY",
        "rule_type": "line_quantity_ordered",
        "name": "Invoiced quantity exceeds the order",
        "description": (
            "Invoiced quantities may not exceed the ordered quantities (partial invoices allowed)."
        ),
        "applies_to": ["INVOICE"],
        "severity": "HIGH",
        "params": {"tolerance": "0", "allow_partial": True},
    },
    {
        "code": "INV_DELIVERED_QUANTITY",
        "rule_type": "line_quantity_delivered",
        "name": "Billed quantity exceeds the delivered quantity",
        "description": (
            "Three-way match: invoiced quantities may not exceed what the delivery notes record."
        ),
        "applies_to": ["INVOICE"],
        "severity": "HIGH",
        "params": {"tolerance": "0"},
    },
    {
        "code": "INV_LINE_NOT_ORDERED",
        "rule_type": "line_not_ordered",
        "name": "Invoice line not on the purchase order",
        "description": "Every invoiced item must appear on the purchase order.",
        "applies_to": ["INVOICE"],
        "severity": "HIGH",
        "params": {},
    },
    {
        "code": "INV_PO_VENDOR",
        "rule_type": "header_match",
        "name": "Vendor differs from the purchase order",
        "description": ("The invoice must come from the vendor the order was placed with."),
        "applies_to": ["INVOICE"],
        "severity": "CRITICAL",
        "params": {"check": "vendor"},
    },
    {
        "code": "INV_PO_CURRENCY",
        "rule_type": "header_match",
        "name": "Currency differs from the purchase order",
        "description": (
            "Invoice and order must use the same currency (amounts are never converted)."
        ),
        "applies_to": ["INVOICE"],
        "severity": "HIGH",
        "params": {"check": "currency"},
    },
    {
        "code": "INV_PO_TAX_RATE",
        "rule_type": "header_match",
        "name": "Tax rate differs from the purchase order",
        "description": "The invoice's tax rate must equal the order's.",
        "applies_to": ["INVOICE"],
        "severity": "MEDIUM",
        "params": {"check": "tax_rate", "tolerance": "0.0001"},
    },
    {
        "code": "INV_PO_PAYMENT_TERMS",
        "rule_type": "header_match",
        "name": "Payment terms differ from the purchase order",
        "description": "The invoice's payment terms should equal the order's.",
        "applies_to": ["INVOICE"],
        "severity": "LOW",
        "params": {"check": "payment_terms"},
    },
    {
        "code": "INV_MISSING_PO",
        "rule_type": "order_reference",
        "name": "Missing purchase order",
        "description": (
            "An invoice must reference a purchase order (fail) that is on file (warn if not yet)."
        ),
        "applies_to": ["INVOICE"],
        "severity": "MEDIUM",
        "params": {"require_reference": True, "require_order_on_file": True},
    },
    {
        "code": "DOC_ARITHMETIC",
        "rule_type": "document_arithmetic",
        "name": "Amounts do not add up",
        "description": (
            "Line amounts, subtotal, tax and total must be consistent on the document itself."
        ),
        "applies_to": ["INVOICE", "PURCHASE_ORDER", "RECEIPT"],
        "severity": "HIGH",
        "params": {"checks": ["LINE_AMOUNT", "LINES_SUM", "TOTAL_ARITHMETIC", "TAX_RATE"]},
    },
    {
        "code": "DOC_MANDATORY_FIELDS",
        "rule_type": "mandatory_fields",
        "name": "Mandatory fields",
        "description": "Fields the business requires per document type.",
        "applies_to": ["CONTRACT", "DELIVERY_NOTE", "INVOICE", "PURCHASE_ORDER", "RECEIPT"],
        "severity": "MEDIUM",
        "params": {
            "fields": {
                "INVOICE": ["vendor_name", "invoice_number", "invoice_date", "total", "currency"],
                "PURCHASE_ORDER": ["po_number", "po_date", "vendor_name", "total"],
                "DELIVERY_NOTE": ["delivery_note_number", "delivery_date", "vendor_name"],
                "RECEIPT": ["merchant_name", "transaction_date", "total"],
                "CONTRACT": ["party_a", "party_b", "effective_date"],
            }
        },
    },
    {
        "code": "DOC_UNKNOWN_VENDOR",
        "rule_type": "known_vendor",
        "name": "Vendor not in the vendor master",
        "description": (
            "The vendor must be a known vendor (add it, or an alias, to the vendor master)."
        ),
        "applies_to": ["DELIVERY_NOTE", "INVOICE", "PURCHASE_ORDER"],
        "severity": "MEDIUM",
        "params": {},
    },
    {
        "code": "DN_PO_QUANTITY",
        "rule_type": "line_quantity_ordered",
        "name": "Delivered quantity exceeds the order",
        "description": (
            "Delivery notes may not record more than was ordered (partial deliveries allowed)."
        ),
        "applies_to": ["DELIVERY_NOTE"],
        "severity": "MEDIUM",
        "params": {"tolerance": "0", "allow_partial": True},
    },
    {
        "code": "DN_LINE_NOT_ORDERED",
        "rule_type": "line_not_ordered",
        "name": "Delivered item not on the purchase order",
        "description": "Every delivered item must appear on the purchase order.",
        "applies_to": ["DELIVERY_NOTE"],
        "severity": "MEDIUM",
        "params": {},
    },
    {
        "code": "DN_PO_VENDOR",
        "rule_type": "header_match",
        "name": "Delivery vendor differs from the purchase order",
        "description": ("The delivery must come from the vendor the order was placed with."),
        "applies_to": ["DELIVERY_NOTE"],
        "severity": "HIGH",
        "params": {"check": "vendor"},
    },
    {
        "code": "DN_MISSING_PO",
        "rule_type": "order_reference",
        "name": "Delivery without purchase order",
        "description": "A delivery note should reference a purchase order on file.",
        "applies_to": ["DELIVERY_NOTE"],
        "severity": "LOW",
        "params": {"require_reference": True, "require_order_on_file": True},
    },
    {
        "code": "CONTRACT_EXPIRY",
        "rule_type": "contract_expiry",
        "name": "Contract expired or expiring",
        "description": (
            "Fails after the expiration date; warns within the notice window or for auto-renewals."
        ),
        "applies_to": ["CONTRACT"],
        "severity": "HIGH",
        "params": {"warn_within_days": 30},
    },
    {
        "code": "POLICY_PAYMENT_TERMS",
        "rule_type": "payment_terms_limit",
        "name": "Payment terms above policy",
        "description": "Company policy caps vendor payment terms.",
        "applies_to": ["INVOICE"],
        "severity": "MEDIUM",
        "params": {"max_days": 60},
    },
]


# Review reasons that existed before this revision, for the backfilled review tasks.
REASON_MESSAGES = {
    "NO_TEXT_FOUND": ("CONTENT", "HIGH", "No readable text was found."),
    "OCR_FAILED": ("CONTENT", "HIGH", "Text recognition failed on at least one page."),
    "LOW_OCR_CONFIDENCE": (
        "CONTENT",
        "MEDIUM",
        "Text recognition confidence is low on at least one page.",
    ),
    "CLASSIFICATION_UNCERTAIN": ("CLASSIFICATION", "MEDIUM", "The document type is uncertain."),
    "EXTRACTION_FAILED": ("EXTRACTION", "HIGH", "No fields could be extracted."),
    "MISSING_REQUIRED_FIELDS": ("EXTRACTION", "HIGH", "Required fields are missing."),
    "EXTRACTION_UNCERTAIN": ("EXTRACTION", "MEDIUM", "Some extracted values are uncertain."),
    "EXTRACTION_INCONSISTENT": (
        "EXTRACTION",
        "HIGH",
        "Extracted amounts or dates do not add up.",
    ),
}


def _enum(values: tuple[str, ...], name: str, length: int = 20) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _timestamp(name: str, default: Any) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=False)


def upgrade() -> None:
    # Key facts of the current extraction on the document (filled by the worker and corrections;
    # `docintel match --all` fills them for documents processed before this revision).
    op.add_column("documents", sa.Column("number_key", sa.String(length=100), nullable=True))
    op.add_column("documents", sa.Column("po_key", sa.String(length=100), nullable=True))
    op.add_column("documents", sa.Column("document_date", sa.Date(), nullable=True))
    op.add_column("documents", sa.Column("total_amount", sa.Numeric(18, 4), nullable=True))
    op.add_column("documents", sa.Column("currency", sa.String(length=3), nullable=True))
    op.add_column("documents", sa.Column("vendor_key", sa.String(length=300), nullable=True))
    op.create_check_constraint(
        op.f("ck_documents_currency_code"),
        "documents",
        "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
    )
    op.create_index(
        op.f("ix_documents_department_po_key"),
        "documents",
        ["department_id", "po_key"],
        postgresql_where=sa.text("deleted_at IS NULL AND po_key IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_documents_department_number_key"),
        "documents",
        ["department_id", "number_key"],
        postgresql_where=sa.text("deleted_at IS NULL AND number_key IS NOT NULL"),
    )

    op.create_table(
        "comparisons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "comparison_type", _enum(COMPARISON_TYPES, "comparison_type", 30), nullable=False
        ),
        sa.Column("origin", _enum(ORIGINS, "origin"), nullable=False),
        sa.Column("subject_document_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("summary", _jsonb(), nullable=False),
        sa.Column("settings", _jsonb(), nullable=False),
        sa.Column("requested_by_id", sa.Uuid(), nullable=True),
        _timestamp("created_at", sa.func.now()),
        sa.ForeignKeyConstraint(
            ["subject_document_id"],
            ["documents.id"],
            name=op.f("fk_comparisons_subject_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_comparisons_department_id_departments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name=op.f("fk_comparisons_requested_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comparisons")),
    )
    op.create_index(
        op.f("uq_comparisons_auto_subject"),
        "comparisons",
        ["subject_document_id"],
        unique=True,
        postgresql_where=sa.text("origin = 'AUTO'"),
    )
    op.create_index(
        op.f("ix_comparisons_department_created"), "comparisons", ["department_id", "created_at"]
    )
    op.create_index(op.f("ix_comparisons_requested_by_id"), "comparisons", ["requested_by_id"])

    op.create_table(
        "comparison_documents",
        sa.Column("comparison_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=True),
        sa.Column("extraction_id", sa.Uuid(), nullable=True),
        sa.Column("role", _enum(ROLES, "role"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["comparison_id"],
            ["comparisons.id"],
            name=op.f("fk_comparison_documents_comparison_id_comparisons"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_comparison_documents_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_comparison_documents_document_version_id_document_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["document_extractions.id"],
            name=op.f("fk_comparison_documents_extraction_id_document_extractions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "comparison_id", "document_id", name=op.f("pk_comparison_documents")
        ),
    )
    op.create_index(
        op.f("ix_comparison_documents_document_id"), "comparison_documents", ["document_id"]
    )
    op.create_index(
        op.f("ix_comparison_documents_extraction_id"), "comparison_documents", ["extraction_id"]
    )
    op.create_index(
        op.f("ix_comparison_documents_version_id"),
        "comparison_documents",
        ["document_version_id"],
    )

    op.create_table(
        "comparison_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("comparison_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("item_key", sa.String(length=200), nullable=False),
        sa.Column("category", _enum(CATEGORIES, "category"), nullable=False),
        sa.Column("check_name", sa.String(length=40), nullable=False),
        sa.Column("line_key", sa.String(length=200), nullable=True),
        sa.Column("status", _enum(ITEM_STATUSES, "status"), nullable=False),
        sa.Column("left_value", sa.Text(), nullable=True),
        sa.Column("right_value", sa.Text(), nullable=True),
        sa.Column("difference", _jsonb(), nullable=True),
        sa.Column("tolerance", _jsonb(), nullable=True),
        sa.Column("evidence", _jsonb(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["comparison_id"],
            ["comparisons.id"],
            name=op.f("fk_comparison_results_comparison_id_comparisons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comparison_results")),
    )
    op.create_index(
        op.f("ix_comparison_results_comparison_id"),
        "comparison_results",
        ["comparison_id", "position"],
    )

    rules = op.create_table(
        "business_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("rule_type", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("applies_to", postgresql.ARRAY(sa.String(length=30)), nullable=False),
        sa.Column("params", _jsonb(), nullable=False),
        sa.Column("severity", _enum(SEVERITIES, "severity"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        _timestamp("created_at", sa.func.now()),
        _timestamp("updated_at", sa.func.now()),
        sa.CheckConstraint("version >= 1", name=op.f("ck_business_rules_version_positive")),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_business_rules_updated_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_business_rules")),
        sa.UniqueConstraint("code", name=op.f("uq_business_rules_code")),
    )
    op.create_index(op.f("ix_business_rules_updated_by_id"), "business_rules", ["updated_by_id"])
    op.bulk_insert(
        rules,
        [
            {"id": _uuid(f"rule:{rule['code']}"), **rule, "is_enabled": True, "version": 1}
            for rule in DEFAULT_RULES
        ],
    )

    op.create_table(
        "rule_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=True),
        sa.Column("comparison_id", sa.Uuid(), nullable=True),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("rule_code", sa.String(length=60), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("outcome", _enum(OUTCOMES, "outcome"), nullable=False),
        sa.Column("severity", _enum(SEVERITIES, "severity"), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("evidence", _jsonb(), nullable=False),
        sa.Column("items", _jsonb(), nullable=False),
        _timestamp("evaluated_at", sa.func.now()),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_rule_results_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_rule_results_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["comparison_id"],
            ["comparisons.id"],
            name=op.f("fk_rule_results_comparison_id_comparisons"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["business_rules.id"],
            name=op.f("fk_rule_results_rule_id_business_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_results")),
    )
    op.create_index(op.f("ix_rule_results_document_id"), "rule_results", ["document_id"])
    op.create_index(op.f("ix_rule_results_rule_id"), "rule_results", ["rule_id"])
    op.create_index(op.f("ix_rule_results_comparison_id"), "rule_results", ["comparison_id"])
    op.create_index(op.f("ix_rule_results_version_id"), "rule_results", ["document_version_id"])

    op.create_table(
        "review_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=True),
        sa.Column("task_type", _enum(TASK_TYPES, "task_type", 30), nullable=False),
        sa.Column("status", _enum(TASK_STATUSES, "status"), nullable=False),
        sa.Column("priority", _enum(PRIORITIES, "priority"), nullable=False),
        sa.Column("reasons", _jsonb(), nullable=False),
        sa.Column("reason_keys", postgresql.ARRAY(sa.String(length=300)), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_to_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", _enum(RESOLUTIONS, "resolution"), nullable=True),
        sa.Column("resolution_note", sa.String(length=1000), nullable=True),
        sa.Column("resolved_by_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at", sa.func.now()),
        _timestamp("updated_at", sa.func.now()),
        sa.CheckConstraint(
            "(status IN ('RESOLVED', 'CANCELLED')) = (resolved_at IS NOT NULL)",
            name=op.f("ck_review_tasks_resolved_at_when_closed"),
        ),
        sa.CheckConstraint(
            "(status = 'RESOLVED') = (resolution IS NOT NULL)",
            name=op.f("ck_review_tasks_resolution_when_resolved"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_review_tasks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_review_tasks_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_id"],
            ["users.id"],
            name=op.f("fk_review_tasks_assigned_to_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_id"],
            ["users.id"],
            name=op.f("fk_review_tasks_resolved_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_tasks")),
    )
    op.create_index(
        op.f("uq_review_tasks_open_document"),
        "review_tasks",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('OPEN', 'IN_PROGRESS')"),
    )
    op.create_index(
        op.f("ix_review_tasks_status_priority_created"),
        "review_tasks",
        ["status", "priority", "created_at"],
    )
    op.create_index(op.f("ix_review_tasks_document_id"), "review_tasks", ["document_id"])
    op.create_index(op.f("ix_review_tasks_version_id"), "review_tasks", ["document_version_id"])
    op.create_index(op.f("ix_review_tasks_assigned_to_id"), "review_tasks", ["assigned_to_id"])
    op.create_index(op.f("ix_review_tasks_resolved_by_id"), "review_tasks", ["resolved_by_id"])

    _backfill_review_tasks()


def _uuid(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"docintel:{name}")


def _backfill_review_tasks() -> None:
    """Documents waiting for review keep waiting: one open task each, from their reasons."""
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, current_version_id, review_reasons FROM documents "
            "WHERE status = 'REVIEW_REQUIRED' AND deleted_at IS NULL"
        )
    ).all()
    for document_id, version_id, codes in rows:
        reasons = [
            {
                "key": f"reason:{code}",
                "category": REASON_MESSAGES[code][0],
                "code": code,
                "severity": REASON_MESSAGES[code][1],
                "message": REASON_MESSAGES[code][2],
            }
            for code in codes or []
            if code in REASON_MESSAGES
        ]
        if not reasons:
            continue
        only_type = {reason["category"] for reason in reasons} == {"CLASSIFICATION"}
        high = any(reason["severity"] == "HIGH" for reason in reasons)
        connection.execute(
            sa.text(
                "INSERT INTO review_tasks (id, document_id, document_version_id, task_type, "
                "status, priority, reasons, reason_keys, created_at, updated_at) VALUES "
                "(gen_random_uuid(), :document_id, :version_id, :task_type, 'OPEN', :priority, "
                "CAST(:reasons AS jsonb), :keys, now(), now())"
            ),
            {
                "document_id": document_id,
                "version_id": version_id,
                "task_type": "CLASSIFICATION_REVIEW" if only_type else "EXTRACTION_REVIEW",
                "priority": "HIGH" if high else "NORMAL",
                "reasons": json.dumps(reasons),
                "keys": [reason["key"] for reason in reasons],
            },
        )


def downgrade() -> None:
    op.drop_table("review_tasks")
    op.drop_table("rule_results")
    op.drop_table("business_rules")
    op.drop_table("comparison_results")
    op.drop_table("comparison_documents")
    op.drop_table("comparisons")
    op.drop_index(op.f("ix_documents_department_number_key"), table_name="documents")
    op.drop_index(op.f("ix_documents_department_po_key"), table_name="documents")
    op.drop_constraint(op.f("ck_documents_currency_code"), "documents", type_="check")
    for column in (
        "vendor_key",
        "currency",
        "total_amount",
        "document_date",
        "po_key",
        "number_key",
    ):
        op.drop_column("documents", column)

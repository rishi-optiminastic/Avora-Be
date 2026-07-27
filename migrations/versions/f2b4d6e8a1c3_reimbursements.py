"""reimbursements table + two-step review notif kinds; merges the two open heads

Revision ID: f2b4d6e8a1c3
Revises: a8d2c4e6f1b3, c7b9d1e3f5a2
Create Date: 2026-07-27 13:30:00.000000

This revision doubles as the merge point for the two previously-open Alembic
heads (a8d2c4e6f1b3, c7b9d1e3f5a2) so `alembic upgrade head` is unambiguous again.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2b4d6e8a1c3"
down_revision: str | Sequence[str] | None = ("a8d2c4e6f1b3", "c7b9d1e3f5a2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    category = sa.Enum(
        "TRAVEL",
        "MEALS",
        "SUPPLIES",
        "SOFTWARE",
        "EQUIPMENT",
        "TRAINING",
        "OTHER",
        name="reimbursementcategory",
    )
    status = sa.Enum(
        "SUBMITTED",
        "MANAGER_APPROVED",
        "APPROVED",
        "REJECTED",
        "WITHDRAWN",
        name="reimbursementstatus",
    )
    category.create(op.get_bind(), checkfirst=True)
    status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "reimbursements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "category",
            postgresql.ENUM(name="reimbursementcategory", create_type=False),
            nullable=False,
            server_default="OTHER",
        ),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("period_month", sa.String(length=7), nullable=False),
        sa.Column("receipt_object_key", sa.String(length=512), nullable=True),
        sa.Column("receipt_filename", sa.String(length=255), nullable=True),
        sa.Column("receipt_content_type", sa.String(length=128), nullable=True),
        sa.Column("receipt_content", sa.LargeBinary(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="reimbursementstatus", create_type=False),
            nullable=False,
            server_default="SUBMITTED",
        ),
        sa.Column("manager_reviewer_id", sa.Uuid(), nullable=True),
        sa.Column("manager_decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manager_note", sa.String(length=1000), nullable=True),
        sa.Column("hr_reviewer_id", sa.Uuid(), nullable=True),
        sa.Column("hr_decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hr_note", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["manager_reviewer_id"], ["employees.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["hr_reviewer_id"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reimbursements_employee_id", "reimbursements", ["employee_id"])
    op.create_index("ix_reimbursements_status", "reimbursements", ["status"])
    op.create_index("ix_reimbursements_period_month", "reimbursements", ["period_month"])
    op.create_index(
        "ix_reimbursements_manager_reviewer_id", "reimbursements", ["manager_reviewer_id"]
    )
    op.create_index("ix_reimbursements_hr_reviewer_id", "reimbursements", ["hr_reviewer_id"])
    op.create_index(
        "ix_reimbursements_employee_status", "reimbursements", ["employee_id", "status"]
    )
    op.create_index(
        "ix_reimbursements_period_status", "reimbursements", ["period_month", "status"]
    )

    for value in ("REIMBURSEMENT_SUBMITTED", "REIMBURSEMENT_DECISION"):
        op.execute(f"ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    op.drop_table("reimbursements")
    sa.Enum(name="reimbursementstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="reimbursementcategory").drop(op.get_bind(), checkfirst=True)
    # NB: the two notificationkind enum values are left in place — Postgres cannot
    # drop an enum value, and leaving them is harmless.

"""eod_reports (End-of-Day draft → approve → send, one per employee/day)

Revision ID: a1b2c3d4e5f6
Revises: 2e859bce2976
Create Date: 2026-06-22 14:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "2e859bce2976"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eod_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("report_date", sa.String(length=10), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "APPROVED",
                "SENT",
                "SKIPPED_ABSENT",
                "FAILED",
                name="eodstatus",
            ),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("edited_summary", sa.Text(), nullable=True),
        sa.Column("highlights", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "employee_id", "report_date", name="uq_eod_reports_employee_report_date"
        ),
    )
    op.create_index("ix_eod_reports_employee_id", "eod_reports", ["employee_id"])
    op.create_index("ix_eod_reports_report_date", "eod_reports", ["report_date"])
    op.create_index("ix_eod_reports_status", "eod_reports", ["status"])


def downgrade() -> None:
    op.drop_index("ix_eod_reports_status", table_name="eod_reports")
    op.drop_index("ix_eod_reports_report_date", table_name="eod_reports")
    op.drop_index("ix_eod_reports_employee_id", table_name="eod_reports")
    op.drop_table("eod_reports")
    sa.Enum(name="eodstatus").drop(op.get_bind(), checkfirst=True)

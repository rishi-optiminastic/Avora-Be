"""payslips — HR-released, immutable per-employee monthly salary-slip snapshots

Revision ID: f0a1b2c3d4e5
Revises: e0d5e771a9c2
Create Date: 2026-06-30

One row per (employee, month), written when HR finalizes/releases a month. The
numbers are frozen here so employees only ever see months HR has released, and
the downloaded/emailed PDF renders from this snapshot. Money in integer minor
units; the full salary breakdown is kept as JSON for the PDF.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f0a1b2c3d4e5"
down_revision: str | None = "e0d5e771a9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sa.Enum("RELEASED", name="payslipstatus").create(op.get_bind(), checkfirst=True)
    status = postgresql.ENUM(name="payslipstatus", create_type=False)

    op.create_table(
        "payslips",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("period_month", sa.String(length=7), nullable=False),
        sa.Column("employee_name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("department", sa.String(length=256), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="INR"),
        sa.Column("monthly_ctc_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("gross_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("net_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("breakdown", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("working_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("present_days", sa.Float(), nullable=False, server_default="0"),
        sa.Column("paid_leave_days", sa.Float(), nullable=False, server_default="0"),
        sa.Column("payable_days", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", status, nullable=False, server_default="RELEASED"),
        sa.Column(
            "released_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["finalized_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "employee_id", "period_month", name="uq_payslips_employee_id_period_month"
        ),
    )
    op.create_index("ix_payslips_employee_id", "payslips", ["employee_id"])
    op.create_index("ix_payslips_period_month", "payslips", ["period_month"])


def downgrade() -> None:
    op.drop_index("ix_payslips_period_month", table_name="payslips")
    op.drop_index("ix_payslips_employee_id", table_name="payslips")
    op.drop_table("payslips")
    sa.Enum(name="payslipstatus").drop(op.get_bind(), checkfirst=True)

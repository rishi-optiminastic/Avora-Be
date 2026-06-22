"""payroll settings (singleton) + payroll runs (HR digest history / idempotency)

Revision ID: c6e7f8a9b0c1
Revises: b5d6e7f8a9c0
Create Date: 2026-06-20 20:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6e7f8a9b0c1"
down_revision: str | None = "b5d6e7f8a9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sa.Enum("MONTHLY", "BIWEEKLY", "WEEKLY", name="paycycle").create(op.get_bind(), checkfirst=True)
    sa.Enum("MANUAL", "AUTO", name="payrollrunsource").create(op.get_bind(), checkfirst=True)
    pay_cycle = postgresql.ENUM(name="paycycle", create_type=False)
    run_source = postgresql.ENUM(name="payrollrunsource", create_type=False)

    op.create_table(
        "payroll_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pay_day_of_month", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="INR"),
        sa.Column("pay_cycle", pay_cycle, nullable=False, server_default="MONTHLY"),
        sa.Column("auto_send_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recipients", sa.String(length=2000), nullable=False, server_default=""),
        sa.Column("basic_pct", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("hra_pct", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("pf_pct", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("pf_cap_minor", sa.BigInteger(), nullable=False, server_default="180000"),
        sa.Column(
            "professional_tax_minor", sa.BigInteger(), nullable=False, server_default="20000"
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["updated_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "payroll_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("period_month", sa.String(length=7), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="INR"),
        sa.Column("total_net_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("employee_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recipients", sa.String(length=2000), nullable=False, server_default=""),
        sa.Column("source", run_source, nullable=False, server_default="MANUAL"),
        sa.Column("triggered_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["triggered_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("period_month", name="uq_payroll_runs_period_month"),
    )
    op.create_index("ix_payroll_runs_period_month", "payroll_runs", ["period_month"])


def downgrade() -> None:
    op.drop_index("ix_payroll_runs_period_month", table_name="payroll_runs")
    op.drop_table("payroll_runs")
    op.drop_table("payroll_settings")
    sa.Enum(name="payrollrunsource").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="paycycle").drop(op.get_bind(), checkfirst=True)

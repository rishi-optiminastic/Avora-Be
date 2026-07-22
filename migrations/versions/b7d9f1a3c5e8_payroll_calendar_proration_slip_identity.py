"""payroll calendar-day proration + richer payslip identity

Revision ID: b7d9f1a3c5e8
Revises: f6a7b8c9d0e1
Create Date: 2026-07-22

Two related changes to how a salary slip is shaped and frozen:

1. Per-employee `location` (e.g. "Mumbai") on `employees`, so the slip header can
   show it alongside job title, department and joining date.
2. Payslip snapshots gain the identity fields the redesigned slip renders from
   (`job_title`, `location`, `hire_date`) plus the calendar-day proration data:
   `total_days` (calendar days in the month — the new proration denominator,
   replacing the old working-days basis for the *ratio* only) and
   `prorated_breakdown` (each earning + PF scaled to actual payable days, with
   Professional Tax / Income Tax kept flat). `working_days` is retained: it still
   drives the present/paid/absent attendance display.

Existing frozen rows are back-filled `total_days = working_days` so their
historical ratio (and displayed denominator) is unchanged — those slips were
prorated on working days and stay honest.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d9f1a3c5e8"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("location", sa.String(length=128), nullable=True))

    op.add_column("payslips", sa.Column("job_title", sa.String(length=128), nullable=True))
    op.add_column("payslips", sa.Column("location", sa.String(length=128), nullable=True))
    op.add_column("payslips", sa.Column("hire_date", sa.Date(), nullable=True))
    op.add_column(
        "payslips",
        sa.Column("total_days", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "payslips",
        sa.Column("prorated_breakdown", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    # Historical slips were prorated on working days; keep their denominator honest.
    op.execute("UPDATE payslips SET total_days = working_days WHERE total_days = 0")


def downgrade() -> None:
    op.drop_column("payslips", "prorated_breakdown")
    op.drop_column("payslips", "total_days")
    op.drop_column("payslips", "hire_date")
    op.drop_column("payslips", "location")
    op.drop_column("payslips", "job_title")
    op.drop_column("employees", "location")

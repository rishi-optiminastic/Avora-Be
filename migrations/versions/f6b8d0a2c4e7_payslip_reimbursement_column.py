"""Freeze the reimbursement amount onto a released payslip.

A slip's net now includes fully-approved expense claims, but its Earnings and
Deductions tables are built from `prorated_breakdown` (which deliberately
excludes them, so nothing is taxed or PF'd). Without this column an old snapshot
would render a Net Pay that doesn't equal Gross minus Deductions. Defaults to 0,
so every slip released before today is still self-consistent.

Revision ID: f6b8d0a2c4e7
Revises: e5a7c9b1d3f6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6b8d0a2c4e7"
down_revision = "e5a7c9b1d3f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payslips",
        sa.Column("reimbursement_minor", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("payslips", "reimbursement_minor")

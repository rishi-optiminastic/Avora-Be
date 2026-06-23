"""leave_policy table + employees.hire_date (joining-anniversary leave year)

Adds the singleton org leave-entitlement config and a nullable joining date on
employees (sourced from the HR sync `start_date`). The leave-balance feature
resets quotas on each employee's joining anniversary; the balance logic falls
back to `created_at` when `hire_date` is unset.

Revision ID: c8d9e0f1a2b3
Revises: b2c3d4e5f6a7
Create Date: 2026-06-22 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("hire_date", sa.Date(), nullable=True))
    op.create_table(
        "leave_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("annual_planned_days", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("annual_sick_days", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["updated_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("leave_policy")
    op.drop_column("employees", "hire_date")

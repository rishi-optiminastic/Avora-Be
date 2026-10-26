"""employees.is_guest - access to a workspace without employment in it

Revision ID: c3e5a7b9d1f2
Revises: b9d1f3a5c7e0
Create Date: 2026-10-26

Granting someone a second workspace used to mean enrolling them in it: the only
way in was an employee row, and an employee row is what payroll, headcount,
attendance and the org chart all count. An HQ admin added to a sister company
would have drawn a payslip from it.

`is_guest` splits the two. Defaults to false, so every existing row keeps the
meaning it already had - a workspace's own staff.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3e5a7b9d1f2"
down_revision = "b9d1f3a5c7e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("is_guest", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_employees_is_guest", "employees", ["is_guest"])


def downgrade() -> None:
    op.drop_index("ix_employees_is_guest", table_name="employees")
    op.drop_column("employees", "is_guest")

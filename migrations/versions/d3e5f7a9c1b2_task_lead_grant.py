"""Task lead grant - see every task and assign to anyone, no HR access.

A Head of Business needs to put work on any department, and the Founder's
Office needs to see everyone's task list. Neither should read salaries, leave
or attendance, so neither may be made HR or admin. `assigns_org_wide` is a
per-person capability, admin-set like `payroll_manager`, that widens tasks and
nothing else.

Builds on the task playbooks migration, which was in progress alongside it;
ship the two together.

Revision ID: d3e5f7a9c1b2
Revises: a1c3e5f7b9d4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3e5f7a9c1b2"
down_revision = "a1c3e5f7b9d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("assigns_org_wide", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("employees", "assigns_org_wide")

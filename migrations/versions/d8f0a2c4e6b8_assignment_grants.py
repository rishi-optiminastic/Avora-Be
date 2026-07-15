"""assignment_grants (explicit "may assign work to" edges between employees)

Revision ID: d8f0a2c4e6b8
Revises: c6f8a0b2d4e6
Create Date: 2026-07-15

The reporting tree can't express authority where a person has two managers who
both hand them work, because an employee has exactly one manager_id. A grant is
that edge stated explicitly. It is assign-only: it does NOT widen the employee
read scope. One row per (assigner, assignee); CASCADE on both sides.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8f0a2c4e6b8"
down_revision: str | None = "c6f8a0b2d4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assignment_grants",
        sa.Column("assigner_id", sa.Uuid(), nullable=False),
        sa.Column("assignee_id", sa.Uuid(), nullable=False),
        sa.Column("granted_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["assigner_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assignee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by_id"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("assigner_id", "assignee_id"),
    )
    op.create_index("ix_assignment_grants_assigner_id", "assignment_grants", ["assigner_id"])
    op.create_index("ix_assignment_grants_assignee_id", "assignment_grants", ["assignee_id"])


def downgrade() -> None:
    op.drop_index("ix_assignment_grants_assignee_id", table_name="assignment_grants")
    op.drop_index("ix_assignment_grants_assigner_id", table_name="assignment_grants")
    op.drop_table("assignment_grants")

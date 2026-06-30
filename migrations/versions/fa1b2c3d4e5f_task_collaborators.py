"""task_collaborators (many-to-many: extra people on a task, view + comment-level)

Revision ID: fa1b2c3d4e5f
Revises: f7a8b9c0d1e2
Create Date: 2026-06-29

A collaborator is someone added to a task besides its assignee. They get the
task in their read scope and may report progress on it; the assignee/assigner of
record is unchanged. One row per (task, employee); CASCADE on both sides.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fa1b2c3d4e5f"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_collaborators",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id", "employee_id"),
    )
    op.create_index("ix_task_collaborators_employee_id", "task_collaborators", ["employee_id"])


def downgrade() -> None:
    op.drop_index("ix_task_collaborators_employee_id", table_name="task_collaborators")
    op.drop_table("task_collaborators")

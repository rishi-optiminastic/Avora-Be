"""richer task fields (expected output, progress, review, blockers, links)

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-06-19 14:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8a9b0c1d2e3"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("expected_output", sa.String(length=2000), nullable=True))
    op.add_column(
        "tasks", sa.Column("completion_pct", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("tasks", sa.Column("review_notes", sa.String(length=2000), nullable=True))
    op.add_column("tasks", sa.Column("final_outcome", sa.String(length=2000), nullable=True))
    op.add_column("tasks", sa.Column("blocked_reason", sa.String(length=500), nullable=True))
    op.add_column(
        "tasks", sa.Column("attachments", sa.JSON(), nullable=False, server_default="[]")
    )
    op.add_column(
        "tasks",
        sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("tasks", sa.Column("parent_task_id", sa.Uuid(), nullable=True))
    op.add_column("tasks", sa.Column("depends_on_id", sa.Uuid(), nullable=True))
    op.create_index("ix_tasks_escalated", "tasks", ["escalated"])
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])
    op.create_foreign_key(
        "fk_tasks_parent_task_id", "tasks", "tasks", ["parent_task_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_tasks_depends_on_id", "tasks", "tasks", ["depends_on_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_depends_on_id", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_parent_task_id", "tasks", type_="foreignkey")
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
    op.drop_index("ix_tasks_escalated", table_name="tasks")
    for col in (
        "depends_on_id",
        "parent_task_id",
        "escalated",
        "attachments",
        "blocked_reason",
        "final_outcome",
        "review_notes",
        "completion_pct",
        "expected_output",
    ):
        op.drop_column("tasks", col)

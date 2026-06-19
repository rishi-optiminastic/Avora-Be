"""task.project_id → work_entities (optional admin-curated project link)

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-06-19 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("project_id", sa.Uuid(), nullable=True),
    )
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])
    op.create_foreign_key(
        "fk_tasks_project_id_work_entities",
        "tasks",
        "work_entities",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_project_id_work_entities", "tasks", type_="foreignkey")
    op.drop_index("ix_tasks_project_id", table_name="tasks")
    op.drop_column("tasks", "project_id")

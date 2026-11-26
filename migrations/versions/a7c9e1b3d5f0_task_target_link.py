"""link a task to a target

Tasks and targets have sat side by side under Goals with nothing joining them,
so there was no way to answer "what work is actually going towards this
number". A nullable FK, mirroring `tasks.project_id`.

SET NULL rather than CASCADE, for the same reason as the project link:
retiring a target must never delete the work that was done towards it. The
record of what somebody worked on outlives the goal it served.

Revision ID: a7c9e1b3d5f0
Revises: f4a6b8d0c2e5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7c9e1b3d5f0"
down_revision = "f4a6b8d0c2e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("target_id", sa.Uuid(), nullable=True))
    op.create_index("ix_tasks_target_id", "tasks", ["target_id"], unique=False)
    op.create_foreign_key(
        "fk_tasks_target_id_targets",
        "tasks",
        "targets",
        ["target_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_target_id_targets", "tasks", type_="foreignkey")
    op.drop_index("ix_tasks_target_id", table_name="tasks")
    op.drop_column("tasks", "target_id")

"""task escalation level

Revision ID: f1a3c5e7b9d2
Revises: e8a0c2d4f6b9
Create Date: 2026-10-05

Adds `tasks.escalation_level` so automatic overdue escalation can fire each tier
exactly once. A plain integer column with a server default, so existing rows
become level 0 (untouched) without a backfill pass.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f1a3c5e7b9d2"
down_revision = "e8a0c2d4f6b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("escalation_level", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_tasks_escalation_level", "tasks", ["escalation_level"])


def downgrade() -> None:
    op.drop_index("ix_tasks_escalation_level", table_name="tasks")
    op.drop_column("tasks", "escalation_level")

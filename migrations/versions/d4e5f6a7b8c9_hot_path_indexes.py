"""hot-path indexes: leave.reviewer_id, task.depends_on_id, eod_reports(status, created_at)

- ix_leaves_reviewer_id      — leave scope clause filters on reviewer_id
- ix_tasks_depends_on_id     — FKs aren't auto-indexed; dependency lookups
- ix_eod_reports_status_created — the overdue-draft sweep (status + created_at);
  replaces the single-column ix_eod_reports_status (the composite covers it).

Revision ID: d4e5f6a7b8c9
Revises: c8d9e0f1a2b3
Create Date: 2026-06-22 20:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_leaves_reviewer_id", "leaves", ["reviewer_id"])
    op.create_index("ix_tasks_depends_on_id", "tasks", ["depends_on_id"])
    op.create_index(
        "ix_eod_reports_status_created", "eod_reports", ["status", "created_at"]
    )
    op.drop_index("ix_eod_reports_status", table_name="eod_reports")


def downgrade() -> None:
    op.create_index("ix_eod_reports_status", "eod_reports", ["status"])
    op.drop_index("ix_eod_reports_status_created", table_name="eod_reports")
    op.drop_index("ix_tasks_depends_on_id", table_name="tasks")
    op.drop_index("ix_leaves_reviewer_id", table_name="leaves")

"""office working-days-per-week + planned-leave min-notice settings

Adds:
- attendance_policy.working_days_per_week (default 5 ⇒ Mon-Fri, backward-compatible)
- leave_policy.planned_min_notice_days (default 2 ⇒ planned leave needs 2 days' notice)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-04 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default backfills the existing singleton row, then is dropped so the
    # column value is owned by the app (the model default) going forward.
    op.add_column(
        "attendance_policy",
        sa.Column("working_days_per_week", sa.Integer(), nullable=False, server_default="5"),
    )
    op.alter_column("attendance_policy", "working_days_per_week", server_default=None)

    op.add_column(
        "leave_policy",
        sa.Column("planned_min_notice_days", sa.Integer(), nullable=False, server_default="2"),
    )
    op.alter_column("leave_policy", "planned_min_notice_days", server_default=None)


def downgrade() -> None:
    op.drop_column("leave_policy", "planned_min_notice_days")
    op.drop_column("attendance_policy", "working_days_per_week")

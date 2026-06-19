"""work_sessions table — explicit clock-in / clock-out

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-06-19 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: str | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "work_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("clock_in_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("clock_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="dashboard"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_work_sessions_employee_id", "work_sessions", ["employee_id"])
    op.create_index(
        "ix_work_sessions_employee_clock_in", "work_sessions", ["employee_id", "clock_in_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_work_sessions_employee_clock_in", table_name="work_sessions")
    op.drop_index("ix_work_sessions_employee_id", table_name="work_sessions")
    op.drop_table("work_sessions")

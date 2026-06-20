"""attendance_policy + regularizations (office timings + regularization credits)

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-06-19 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attendance_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_start_minute", sa.Integer(), nullable=False, server_default="540"),
        sa.Column("work_end_minute", sa.Integer(), nullable=False, server_default="1080"),
        sa.Column("buffer_minutes", sa.Integer(), nullable=False, server_default="15"),
        sa.Column(
            "regularization_window_minutes", sa.Integer(), nullable=False, server_default="30"
        ),
        sa.Column("full_day_min_minutes", sa.Integer(), nullable=False, server_default="480"),
        sa.Column("half_day_min_minutes", sa.Integer(), nullable=False, server_default="240"),
        sa.Column("monthly_regularizations", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["updated_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    sa.Enum("PENDING", "APPROVED", "REJECTED", name="regularizationstatus").create(
        op.get_bind(), checkfirst=True
    )
    status = postgresql.ENUM(name="regularizationstatus", create_type=False)
    op.create_table(
        "regularizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("status", status, nullable=False, server_default="PENDING"),
        sa.Column("reviewed_by_id", sa.Uuid(), nullable=True),
        sa.Column("review_note", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_regularizations_employee_id", "regularizations", ["employee_id"])
    op.create_index("ix_regularizations_status", "regularizations", ["status"])
    op.create_index("ix_regularizations_employee_day", "regularizations", ["employee_id", "day"])


def downgrade() -> None:
    op.drop_index("ix_regularizations_employee_day", table_name="regularizations")
    op.drop_index("ix_regularizations_status", table_name="regularizations")
    op.drop_index("ix_regularizations_employee_id", table_name="regularizations")
    op.drop_table("regularizations")
    sa.Enum(name="regularizationstatus").drop(op.get_bind(), checkfirst=True)
    op.drop_table("attendance_policy")

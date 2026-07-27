"""attendance_overrides table (HR/Admin manual day-status corrections)

Revision ID: c5e7f9a1b3d6
Revises: b4d6f8a0c2e5
Create Date: 2026-07-27 15:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5e7f9a1b3d6"
down_revision: str | Sequence[str] | None = "b4d6f8a0c2e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    status = sa.Enum("FULL_DAY", "HALF_DAY", "ABSENT", name="attendanceoverridestatus")
    status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "attendance_overrides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="attendanceoverridestatus", create_type=False),
            nullable=False,
        ),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_id", "day", name="uq_attendance_overrides_employee_day"),
    )
    op.create_index(
        "ix_attendance_overrides_employee_id", "attendance_overrides", ["employee_id"]
    )
    op.create_index(
        "ix_attendance_overrides_employee_day", "attendance_overrides", ["employee_id", "day"]
    )


def downgrade() -> None:
    op.drop_table("attendance_overrides")
    sa.Enum(name="attendanceoverridestatus").drop(op.get_bind(), checkfirst=True)

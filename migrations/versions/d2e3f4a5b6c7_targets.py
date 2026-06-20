"""targets table (monthly/quarterly goals + milestones)

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-06-19 17:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sa.Enum("MONTHLY", "QUARTERLY", name="targetperiod").create(op.get_bind(), checkfirst=True)
    sa.Enum(
        "PLANNED", "ON_TRACK", "AT_RISK", "ACHIEVED", "MISSED", name="targetstatus"
    ).create(op.get_bind(), checkfirst=True)
    period = postgresql.ENUM("MONTHLY", "QUARTERLY", name="targetperiod", create_type=False)
    status = postgresql.ENUM(name="targetstatus", create_type=False)
    op.create_table(
        "targets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("period", period, nullable=False, server_default="MONTHLY"),
        sa.Column("department", sa.String(length=128), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_by_id", sa.Uuid(), nullable=True),
        sa.Column("metric", sa.String(length=128), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("target_value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("current_value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_result", sa.String(length=2000), nullable=True),
        sa.Column("status", status, nullable=False, server_default="PLANNED"),
        sa.Column("review_notes", sa.String(length=2000), nullable=True),
        sa.Column("member_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("parent_target_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["owner_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_by_id"], ["employees.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_target_id"], ["targets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_targets_owner_id", "targets", ["owner_id"])
    op.create_index("ix_targets_owner_status", "targets", ["owner_id", "status"])
    op.create_index("ix_targets_period", "targets", ["period"])
    op.create_index("ix_targets_department", "targets", ["department"])
    op.create_index("ix_targets_due_date", "targets", ["due_date"])
    op.create_index("ix_targets_status", "targets", ["status"])
    op.create_index("ix_targets_parent_target_id", "targets", ["parent_target_id"])


def downgrade() -> None:
    for idx in (
        "ix_targets_parent_target_id",
        "ix_targets_status",
        "ix_targets_due_date",
        "ix_targets_department",
        "ix_targets_period",
        "ix_targets_owner_status",
        "ix_targets_owner_id",
    ):
        op.drop_index(idx, table_name="targets")
    op.drop_table("targets")
    sa.Enum(name="targetstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="targetperiod").drop(op.get_bind(), checkfirst=True)

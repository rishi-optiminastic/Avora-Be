"""notifications (in-app inbox: leave/task/underperformance delivery)

Revision ID: c7d8e9f0a1b2
Revises: a4b5c6d7e8f9
Create Date: 2026-06-20 21:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sa.Enum(
        "LEAVE_REQUEST",
        "LEAVE_DECISION",
        "TASK_ASSIGNED",
        "UNDERPERFORMANCE",
        "ESCALATION",
        "SYSTEM",
        name="notificationkind",
    ).create(op.get_bind(), checkfirst=True)
    sa.Enum("INFO", "WARNING", "CRITICAL", name="notificationlevel").create(
        op.get_bind(), checkfirst=True
    )
    kind = postgresql.ENUM(name="notificationkind", create_type=False)
    level = postgresql.ENUM(name="notificationlevel", create_type=False)

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("kind", kind, nullable=False, server_default="SYSTEM"),
        sa.Column("level", level, nullable=False, server_default="INFO"),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=True),
        sa.Column("link", sa.String(length=300), nullable=True),
        sa.Column("entity_type", sa.String(length=40), nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["recipient_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_recipient_id", "notifications", ["recipient_id"])
    op.create_index("ix_notifications_kind", "notifications", ["kind"])
    op.create_index("ix_notifications_recipient_read", "notifications", ["recipient_id", "read_at"])
    op.create_index(
        "ix_notifications_dedupe",
        "notifications",
        ["recipient_id", "kind", "entity_type", "entity_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_dedupe", table_name="notifications")
    op.drop_index("ix_notifications_recipient_read", table_name="notifications")
    op.drop_index("ix_notifications_kind", table_name="notifications")
    op.drop_index("ix_notifications_recipient_id", table_name="notifications")
    op.drop_table("notifications")
    sa.Enum(name="notificationlevel").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="notificationkind").drop(op.get_bind(), checkfirst=True)

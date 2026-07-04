"""announcements — org-wide dashboard banners posted by HR/Admin

Revision ID: a1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-07-01

One row per posted announcement. Shown in the dashboard bar to every employee
while `active` and not past `expires_at`. Derived "holiday tomorrow" notices are
computed at read time and are NOT stored here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a8b7c6d5e4f"
down_revision: str | None = "3d584694fdb0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    sa.Enum("INFO", "SUCCESS", "WARNING", "CRITICAL", name="announcementlevel").create(
        bind, checkfirst=True
    )
    # Idempotent: skip if the table already exists (created out-of-band on some DBs).
    if sa.inspect(bind).has_table("announcements"):
        return
    level = postgresql.ENUM(name="announcementlevel", create_type=False)

    op.create_table(
        "announcements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("level", level, nullable=False, server_default="INFO"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_announcements_active", "announcements", ["active"])


def downgrade() -> None:
    op.drop_index("ix_announcements_active", table_name="announcements")
    op.drop_table("announcements")
    sa.Enum(name="announcementlevel").drop(op.get_bind(), checkfirst=True)

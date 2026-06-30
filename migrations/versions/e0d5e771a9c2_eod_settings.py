"""eod_settings table (org-wide EOD schedule: toggle + draft/send times)

Backs EodSettingsService: a single row holding the EOD on/off switch and the two
local-clock times (draft, send) that drive the scheduler. Moves the schedule out
of env so Admin/HR can tune it from Settings with no redeploy. The row is seeded
from env on first read, so an existing deployment keeps its current behaviour.

Revision ID: e0d5e771a9c2
Revises: fc3d4e5f6a7b
Create Date: 2026-06-30 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e0d5e771a9c2"
down_revision: str | None = "fc3d4e5f6a7b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eod_settings",
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("draft_hour", sa.Integer(), server_default="16", nullable=False),
        sa.Column("draft_minute", sa.Integer(), server_default="30", nullable=False),
        sa.Column("send_hour", sa.Integer(), server_default="18", nullable=False),
        sa.Column("send_minute", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["employees.id"],
            name=op.f("fk_eod_settings_updated_by_employees"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eod_settings")),
    )


def downgrade() -> None:
    op.drop_table("eod_settings")

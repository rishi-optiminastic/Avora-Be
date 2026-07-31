"""notificationkind: add task_comment (notify task participants on a new message)

Revision ID: e7a9c1b3d5f8
Revises: d6f8a0b2c4e7
Create Date: 2026-07-27 16:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e7a9c1b3d5f8"
down_revision: str | Sequence[str] | None = "d6f8a0b2c4e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS 'task_comment'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; leaving it is harmless.
    pass

"""notification kind: add 'APPRECIATION' (manager kudos on task completion)

Revision ID: b1d3f5a7c9e2
Revises: a2c4e6f8b1d3
Create Date: 2026-07-11 11:55:00.000000

The `notificationkind` native enum stores member NAMES, so the new member adds
the label 'APPRECIATION'. Postgres 12+ allows ADD VALUE inside a tx as long as
the value isn't used in the same tx (it isn't here).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b1d3f5a7c9e2"
down_revision: str | None = "a2c4e6f8b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS 'APPRECIATION'")


def downgrade() -> None:
    # Postgres can't drop a single enum value without recreating the type; the
    # extra label is harmless, so this is a no-op.
    pass

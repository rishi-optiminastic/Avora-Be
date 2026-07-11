"""task cadence: add 'one_time' (the new default)

Revision ID: a9b8c7d6e5f4
Revises: f6a7b8c9d0e1
Create Date: 2026-07-10 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a9b8c7d6e5f4"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add the new enum value. PostgreSQL 12+ allows ADD VALUE inside a
    # transaction as long as the value is not USED in the same transaction
    # (it isn't here). The column's default is applied ORM-side, so no
    # server_default change is needed. Existing rows keep their cadence.
    op.execute("ALTER TYPE taskcadence ADD VALUE IF NOT EXISTS 'ONE_TIME'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type, and rows may already
    # reference 'ONE_TIME'. Leaving the value in place is harmless — no-op.
    pass

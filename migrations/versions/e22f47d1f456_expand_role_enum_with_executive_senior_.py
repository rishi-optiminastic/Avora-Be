"""expand role enum with executive, senior_manager, it_admin, viewer

Revision ID: e22f47d1f456
Revises: 9b12b308522e
Create Date: 2026-06-13 14:37:44.526878
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e22f47d1f456"
down_revision: str | None = "9b12b308522e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_VALUES = ("EXECUTIVE", "SENIOR_MANAGER", "IT_ADMIN", "VIEWER")


def upgrade() -> None:
    # The `role` column is a native Postgres ENUM; new members are added with
    # ALTER TYPE ... ADD VALUE (Postgres 12+ allows this inside a transaction as
    # long as the new value isn't used in the same transaction — it isn't here).
    # Values are the StrEnum member names, matching the existing enum labels.
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE role ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum type without rebuilding it; the
    # added members are harmless if unused, so this is intentionally a no-op.
    pass

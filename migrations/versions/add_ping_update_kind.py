"""ping kind: add 'update' (on-demand agent self-update command)

Revision ID: add_ping_update_kind
Revises: d4e5f6a7b8c9
Create Date: 2026-06-23

The `pingkind` native enum stores member NAMES (PING, CAPTURE, …), so the new
member adds the label 'UPDATE'. Postgres 12+ allows ADD VALUE inside a tx as
long as the value isn't used in the same tx (it isn't here).
"""

from __future__ import annotations

from alembic import op

revision = "add_ping_update_kind"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE pingkind ADD VALUE IF NOT EXISTS 'UPDATE'")


def downgrade() -> None:
    # Postgres can't drop a single enum value without recreating the type; the
    # extra label is harmless, so this is a no-op.
    pass

"""employee tracking_mode (work/personal) + mode_* ping kinds

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-06-19 12:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    mode = sa.Enum("WORK", "PERSONAL", name="trackingmode")
    mode.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "employees",
        sa.Column("tracking_mode", mode, nullable=False, server_default="WORK"),
    )
    # New server→agent commands to pause/resume capture.
    op.execute("ALTER TYPE pingkind ADD VALUE IF NOT EXISTS 'MODE_WORK'")
    op.execute("ALTER TYPE pingkind ADD VALUE IF NOT EXISTS 'MODE_PERSONAL'")


def downgrade() -> None:
    op.drop_column("employees", "tracking_mode")
    sa.Enum(name="trackingmode").drop(op.get_bind(), checkfirst=True)
    # Postgres can't drop individual enum values; the unused MODE_* labels on
    # pingkind are left in place (harmless).

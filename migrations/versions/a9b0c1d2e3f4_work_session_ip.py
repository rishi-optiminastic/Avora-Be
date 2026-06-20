"""work_session ip_address + location (login/logout management)

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-06-19 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9b0c1d2e3f4"
down_revision: str | None = "f8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("work_sessions", sa.Column("ip_address", sa.String(length=64), nullable=True))
    op.add_column("work_sessions", sa.Column("location", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("work_sessions", "location")
    op.drop_column("work_sessions", "ip_address")

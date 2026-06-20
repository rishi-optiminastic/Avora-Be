"""ping kind (ping vs capture command)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-06-18 17:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    kind = sa.Enum("PING", "CAPTURE", name="pingkind")
    kind.create(op.get_bind(), checkfirst=True)
    op.add_column("pings", sa.Column("kind", kind, nullable=False, server_default="PING"))


def downgrade() -> None:
    op.drop_column("pings", "kind")
    sa.Enum(name="pingkind").drop(op.get_bind(), checkfirst=True)

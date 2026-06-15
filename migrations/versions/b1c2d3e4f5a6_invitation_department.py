"""invitation department column

Revision ID: b1c2d3e4f5a6
Revises: a06e746eedef
Create Date: 2026-06-15 10:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a06e746eedef"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invitations",
        sa.Column("department", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invitations", "department")

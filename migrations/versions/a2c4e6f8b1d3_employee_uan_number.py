"""employee uan_number (EPF Universal Account Number)

Revision ID: a2c4e6f8b1d3
Revises: d1e2f3a4b5c7
Create Date: 2026-07-11 11:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2c4e6f8b1d3"
down_revision: str | None = "d1e2f3a4b5c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("uan_number", sa.String(length=12), nullable=True))


def downgrade() -> None:
    op.drop_column("employees", "uan_number")

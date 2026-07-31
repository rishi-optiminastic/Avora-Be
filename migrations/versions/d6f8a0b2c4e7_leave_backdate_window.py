"""leave_policy.max_backdate_days — configurable backdating window

Revision ID: d6f8a0b2c4e7
Revises: c5e7f9a1b3d6
Create Date: 2026-07-27 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6f8a0b2c4e7"
down_revision: str | Sequence[str] | None = "c5e7f9a1b3d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "leave_policy",
        sa.Column("max_backdate_days", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("leave_policy", "max_backdate_days")

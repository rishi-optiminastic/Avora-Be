"""leaves.half_day_period (first/second half of a half-day leave)

Revision ID: b3c4d5e6f7a8
Revises: 9a8b7c6d5e4f
Create Date: 2026-07-02 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "9a8b7c6d5e4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sa.Enum("FIRST_HALF", "SECOND_HALF", name="halfdayperiod").create(
        op.get_bind(), checkfirst=True
    )
    half_day_period = postgresql.ENUM(name="halfdayperiod", create_type=False)
    op.add_column("leaves", sa.Column("half_day_period", half_day_period, nullable=True))


def downgrade() -> None:
    op.drop_column("leaves", "half_day_period")
    sa.Enum(name="halfdayperiod").drop(op.get_bind(), checkfirst=True)

"""leave_allocations: days taken before Avora held leave records

HR used to express "they already used their 4 sick days in Zoho" by lowering
the person's sick quota to 0, which then read as "no sick leave" on every
screen. These columns record the days as USED instead, so the card shows the
real quota, the real usage and the real remainder. They belong to one leave
year (`used_before_year_start`) and count only inside it.

Revision ID: b2d4f6a8c0e2
Revises: 626c7efe6527
Create Date: 2026-11-06 09:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d4f6a8c0e2"
down_revision: str | None = "626c7efe6527"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TYPES = (
    "planned",
    "sick",
    "annual",
    "bereavement",
    "birthday",
    "maternity",
    "paternity",
    "marriage",
)


def upgrade() -> None:
    for leave_type in _TYPES:
        op.add_column(
            "leave_allocations",
            sa.Column(f"{leave_type}_used_before", sa.Numeric(5, 1), nullable=True),
        )
    op.add_column(
        "leave_allocations", sa.Column("used_before_year_start", sa.Date(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("leave_allocations", "used_before_year_start")
    for leave_type in reversed(_TYPES):
        op.drop_column("leave_allocations", f"{leave_type}_used_before")

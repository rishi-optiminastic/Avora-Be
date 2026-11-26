"""leave quota overrides allow half days

A per-employee quota is genuinely fractional. Planned leave pro-rated from a
mid-year confirmation lands on 4.5 days as often as 4, and the integer column
truncated it on write: somebody confirmed on 17 November was owed 4.5 days,
was stored 4, and nothing anywhere reported the loss.

Numeric(5,1) matches the `*_used_before` columns on the same table, which have
always allowed halves - so a quota and the usage counted against it are now
the same shape.

Widening is safe in both directions here: every existing value is a whole
number, so the downgrade back to Integer cannot lose anything that was there
before this migration. It CAN lose a half day entered afterwards, which is why
the downgrade rounds rather than truncating.

Revision ID: b8d0f2a4c6e1
Revises: a7c9e1b3d5f0
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8d0f2a4c6e1"
down_revision = "a7c9e1b3d5f0"
branch_labels = None
depends_on = None

COLUMNS = (
    "planned_days",
    "sick_days",
    "annual_days",
    "bereavement_days",
    "birthday_days",
    "maternity_days",
    "paternity_days",
    "marriage_days",
)


def upgrade() -> None:
    for column in COLUMNS:
        op.alter_column(
            "leave_allocations",
            column,
            existing_type=sa.Integer(),
            type_=sa.Numeric(5, 1),
            existing_nullable=True,
        )


def downgrade() -> None:
    for column in COLUMNS:
        op.alter_column(
            "leave_allocations",
            column,
            existing_type=sa.Numeric(5, 1),
            type_=sa.Integer(),
            existing_nullable=True,
            # ROUND, not truncate: a half day entered while this migration was
            # applied should come back as the nearer whole day rather than
            # silently losing somebody half a day of leave.
            postgresql_using=f"ROUND({column})::integer",
        )

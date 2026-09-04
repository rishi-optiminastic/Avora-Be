"""Bring the leave policy in line with the written one.

The stored row still carried the original placeholders (planned 12, annual 15,
sick 8, bereavement 5, paternity 15). The org's written policy is planned 8,
annual 6, sick 6, bereavement 3, paternity 7 — so every tenured employee's
balance was overstated on five leave types.

Changing the model defaults only affects NEW rows, and there is exactly one
policy row that already exists, so it has to be updated here. HR can still
change any of these in Settings afterwards; this only corrects the starting
point. Maternity is left alone pending a decision on what "as per applicable
law" should be stored as.

Revision ID: d1f3a5c7e9b2
Revises: c9e1b3d5f7a2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1f3a5c7e9b2"
down_revision = "c9e1b3d5f7a2"
branch_labels = None
depends_on = None

# (column, written policy, the placeholder it replaces)
_VALUES = (
    ("annual_planned_days", 8, 12),
    ("annual_days", 6, 15),
    ("annual_sick_days", 6, 8),
    ("bereavement_days", 3, 5),
    ("paternity_days", 7, 15),
)


def upgrade() -> None:
    for column, written, placeholder in _VALUES:
        # Only move rows still sitting on the placeholder — if HR has already
        # tuned a value by hand, that decision outranks this migration.
        op.execute(
            sa.text(
                f"UPDATE leave_policies SET {column} = :written "  # noqa: S608
                f"WHERE {column} = :placeholder"
            ).bindparams(written=written, placeholder=placeholder)
        )


def downgrade() -> None:
    for column, written, placeholder in _VALUES:
        op.execute(
            sa.text(
                f"UPDATE leave_policies SET {column} = :placeholder "  # noqa: S608
                f"WHERE {column} = :written"
            ).bindparams(written=written, placeholder=placeholder)
        )

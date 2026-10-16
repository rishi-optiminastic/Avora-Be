"""What the generated probation letters need on the decision row.

`original_end_date` is the end date an extension replaced, and `expectations`
the numbered list HR writes into the extension letter. Both are nullable: only
an extension uses them, and decisions recorded before this have neither.

Revision ID: c8f0b2d4e6a9
Revises: b7e9a1c3d5f8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c8f0b2d4e6a9"
down_revision = "b7e9a1c3d5f8"
branch_labels = None
depends_on = None

_TABLE = "probation_decisions"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("original_end_date", sa.Date(), nullable=True))
    op.add_column(_TABLE, sa.Column("expectations", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "expectations")
    op.drop_column(_TABLE, "original_end_date")

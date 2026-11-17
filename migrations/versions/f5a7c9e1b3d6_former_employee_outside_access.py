"""Outside access for a former employee - sign in without becoming a guest.

Revision ID: f5a7c9e1b3d6
Revises: e4f6a8b0c2d5
Create Date: 2026-11-17

A person transferred to a sister workspace keeps an inactive employee row here.
Giving them outside access used to mean turning that row into a guest, which
would take the payslips, attendance and leave it carries out of this
workspace's figures, so it was refused outright. This flag lets the inactive
row sign in instead, leaving `is_guest` and the history alone.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f5a7c9e1b3d6"
down_revision = "e4f6a8b0c2d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("outside_access", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("employees", "outside_access")

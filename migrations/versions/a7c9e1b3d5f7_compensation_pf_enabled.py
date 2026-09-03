"""Per-employee Provident Fund switch.

Whether PF applies is a fact about someone's contract (consultants, and staff
above the wage ceiling who opted out), not an org-wide policy. Defaults to true
with a server default so every existing compensation row keeps deducting PF
exactly as it does today — this migration changes nobody's pay on its own.

Revision ID: a7c9e1b3d5f7
Revises: f6b8d0a2c4e7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7c9e1b3d5f7"
down_revision = "f6b8d0a2c4e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compensations",
        sa.Column("pf_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("compensations", "pf_enabled")

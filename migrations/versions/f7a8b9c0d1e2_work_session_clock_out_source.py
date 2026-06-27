"""work_sessions: add clock_out_source

Revision ID: f7a8b9c0d1e2
Revises: eecb30a20328
Create Date: 2026-06-27

Records HOW a session was clocked OUT (separately from how it was clocked in):
'dashboard' (the person clicked it), 'biometric' (device out-punch), or 'auto'
(the auto-checkout worker closed a forgotten session). NULL while still open.
Additive, nullable — safe to apply online.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "eecb30a20328"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "work_sessions",
        sa.Column("clock_out_source", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("work_sessions", "clock_out_source")

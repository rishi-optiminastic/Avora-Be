"""payroll_settings: auto_release_enabled + auto_release_day

Revision ID: a9c1e3f5b7d2
Revises: f8b0d2c4e6a1
Create Date: 2026-07-31 12:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c1e3f5b7d2"
down_revision: str | Sequence[str] | None = "f8b0d2c4e6a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payroll_settings",
        sa.Column(
            "auto_release_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "payroll_settings",
        sa.Column("auto_release_day", sa.Integer(), nullable=False, server_default="8"),
    )


def downgrade() -> None:
    op.drop_column("payroll_settings", "auto_release_day")
    op.drop_column("payroll_settings", "auto_release_enabled")

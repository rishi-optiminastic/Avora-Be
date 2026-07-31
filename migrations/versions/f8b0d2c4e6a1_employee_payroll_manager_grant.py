"""employee.payroll_manager — per-user grant to manage the payroll cluster

Revision ID: f8b0d2c4e6a1
Revises: e7a9c1b3d5f8
Create Date: 2026-07-31 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8b0d2c4e6a1"
down_revision: str | Sequence[str] | None = "e7a9c1b3d5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column(
            "payroll_manager", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("employees", "payroll_manager")

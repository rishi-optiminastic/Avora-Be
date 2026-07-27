"""payroll register fields: employees.employee_number + compensations.payment_mode

Revision ID: a3c5e7f9b2d4
Revises: f2b4d6e8a1c3
Create Date: 2026-07-27 14:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c5e7f9b2d4"
down_revision: str | Sequence[str] | None = "f2b4d6e8a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "employees", sa.Column("employee_number", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "compensations",
        sa.Column(
            "payment_mode",
            sa.String(length=64),
            nullable=False,
            server_default="Bank Transfer",
        ),
    )


def downgrade() -> None:
    op.drop_column("compensations", "payment_mode")
    op.drop_column("employees", "employee_number")

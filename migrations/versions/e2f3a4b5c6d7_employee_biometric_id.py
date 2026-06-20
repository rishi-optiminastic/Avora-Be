"""employee biometric_id (office attendance-device enrollment id)

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-06-20 22:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("biometric_id", sa.String(length=64), nullable=True))
    op.create_index("ix_employees_biometric_id", "employees", ["biometric_id"])


def downgrade() -> None:
    op.drop_index("ix_employees_biometric_id", table_name="employees")
    op.drop_column("employees", "biometric_id")

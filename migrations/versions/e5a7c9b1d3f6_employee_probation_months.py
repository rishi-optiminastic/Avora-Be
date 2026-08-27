"""employees.probation_months — per-person probation length override

Probation is negotiated at offer stage: a senior hire may get three months where
the org default is six. NULL keeps the org-wide `leave_policy.probation_months`,
so existing employees are unaffected.

Revision ID: e5a7c9b1d3f6
Revises: d4f6b8c0e2a5
Create Date: 2026-08-27 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a7c9b1d3f6"
down_revision: str | Sequence[str] | None = "d4f6b8c0e2a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("probation_months", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("employees", "probation_months")

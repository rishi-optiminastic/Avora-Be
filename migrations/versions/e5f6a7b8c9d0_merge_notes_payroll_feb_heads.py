"""merge notes + payroll-feb heads

Revision ID: e5f6a7b8c9d0
Revises: d5e6f7a8b9c0, d7e8f9a0b1c2
Create Date: 2026-07-03 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f6a7b8c9d0'
down_revision: str | None = ('d5e6f7a8b9c0', 'd7e8f9a0b1c2')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

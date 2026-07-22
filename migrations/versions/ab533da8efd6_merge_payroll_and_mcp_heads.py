"""merge payroll and mcp heads

Revision ID: ab533da8efd6
Revises: b7d9f1a3c5e8, d8f0a2c4e6b8
Create Date: 2026-07-22 13:52:11.791763
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'ab533da8efd6'
down_revision: str | None = ('b7d9f1a3c5e8', 'd8f0a2c4e6b8')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

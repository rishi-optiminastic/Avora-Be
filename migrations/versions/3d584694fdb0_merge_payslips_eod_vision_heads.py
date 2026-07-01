"""merge payslips + eod-vision heads

Revision ID: 3d584694fdb0
Revises: f0a1b2c3d4e5, a7c1d2e3f4b5
Create Date: 2026-07-01 09:57:15.444171
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '3d584694fdb0'
down_revision: str | None = ('f0a1b2c3d4e5', 'a7c1d2e3f4b5')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

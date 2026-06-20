"""activity_samples page_title + browser (browsing log enrichment)

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-06-19 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b0c1d2e3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("activity_samples", sa.Column("page_title", sa.String(length=512), nullable=True))
    op.add_column("activity_samples", sa.Column("browser", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("activity_samples", "browser")
    op.drop_column("activity_samples", "page_title")

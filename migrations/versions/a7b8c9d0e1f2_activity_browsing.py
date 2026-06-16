"""activity browsing: url + domain columns

Revision ID: a7b8c9d0e1f2
Revises: f5a6b7c8d9e0
Create Date: 2026-06-16 13:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f5a6b7c8d9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("activity_samples", sa.Column("url", sa.String(length=2048), nullable=True))
    op.add_column("activity_samples", sa.Column("domain", sa.String(length=255), nullable=True))
    op.create_index("ix_activity_samples_domain", "activity_samples", ["domain"])


def downgrade() -> None:
    op.drop_index("ix_activity_samples_domain", table_name="activity_samples")
    op.drop_column("activity_samples", "domain")
    op.drop_column("activity_samples", "url")

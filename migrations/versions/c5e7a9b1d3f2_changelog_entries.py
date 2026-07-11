"""changelog_entries (admin-published product updates)

Revision ID: c5e7a9b1d3f2
Revises: b1d3f5a7c9e2
Create Date: 2026-07-11 12:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e7a9b1d3f2"
down_revision: str | None = "b1d3f5a7c9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "changelog_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="feature"),
        sa.Column("version", sa.String(length=40), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_changelog_entries_category", "changelog_entries", ["category"])


def downgrade() -> None:
    op.drop_index("ix_changelog_entries_category", table_name="changelog_entries")
    op.drop_table("changelog_entries")

"""category_rules table (configurable productivity classification)

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-06-19 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    sa.Enum("DOMAIN", "APP", name="matchtype").create(bind, checkfirst=True)
    sa.Enum(
        "PRODUCTIVE",
        "NEUTRAL",
        "UNPRODUCTIVE",
        "IDLE",
        "UNKNOWN",
        "REQUIRES_REVIEW",
        name="productivitycategory",
    ).create(bind, checkfirst=True)
    # Reference the now-existing types WITHOUT re-emitting CREATE TYPE.
    match_type = postgresql.ENUM("DOMAIN", "APP", name="matchtype", create_type=False)
    category = postgresql.ENUM(name="productivitycategory", create_type=False)
    op.create_table(
        "category_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("match_type", match_type, nullable=False),
        sa.Column("pattern", sa.String(length=256), nullable=False),
        sa.Column("category", category, nullable=False),
        sa.Column("department", sa.String(length=128), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["employees.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_category_rules_department", "category_rules", ["department"])


def downgrade() -> None:
    op.drop_index("ix_category_rules_department", table_name="category_rules")
    op.drop_table("category_rules")
    sa.Enum(name="productivitycategory").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="matchtype").drop(op.get_bind(), checkfirst=True)

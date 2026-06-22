"""org settings (workspace identity / white-label singleton)

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-06-21 21:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "org_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("location", sa.String(length=120), nullable=False),
        sa.Column("product_name", sa.String(length=60), nullable=False),
        sa.Column("logo_object_key", sa.String(length=512), nullable=True),
        sa.Column("logo_content", sa.LargeBinary(), nullable=True),
        sa.Column("logo_content_type", sa.String(length=64), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["employees.id"],
            name=op.f("fk_org_settings_updated_by_employees"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_settings")),
    )


def downgrade() -> None:
    op.drop_table("org_settings")

"""onboarding config (org-wide setup checklist singleton)

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-06-21 21:45:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboarding_config",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "eyebrow", sa.String(length=120), nullable=False, server_default="Welcome to Avora"
        ),
        sa.Column(
            "title", sa.String(length=200), nullable=False, server_default="Let's get you set up"
        ),
        sa.Column("subtitle", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("steps", sa.JSON(), nullable=False, server_default="[]"),
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
            name=op.f("fk_onboarding_config_updated_by_employees"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_onboarding_config")),
    )


def downgrade() -> None:
    op.drop_table("onboarding_config")

"""personal access tokens

Revision ID: c6f8a0b2d4e6
Revises: c5e7a9b1d3f2
Create Date: 2026-07-11 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6f8a0b2d4e6"
down_revision: str | None = "c5e7a9b1d3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "personal_access_tokens",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=256), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_personal_access_tokens_employee_id_employees"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_personal_access_tokens")),
    )
    op.create_index(
        op.f("ix_personal_access_tokens_employee_id"),
        "personal_access_tokens",
        ["employee_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_personal_access_tokens_is_revoked"),
        "personal_access_tokens",
        ["is_revoked"],
        unique=False,
    )
    op.create_index(
        op.f("ix_personal_access_tokens_token_hash"),
        "personal_access_tokens",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_personal_access_tokens_token_hash"), table_name="personal_access_tokens"
    )
    op.drop_index(
        op.f("ix_personal_access_tokens_is_revoked"), table_name="personal_access_tokens"
    )
    op.drop_index(
        op.f("ix_personal_access_tokens_employee_id"), table_name="personal_access_tokens"
    )
    op.drop_table("personal_access_tokens")

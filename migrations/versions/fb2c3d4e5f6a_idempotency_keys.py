"""idempotency_keys table (generic replay-proof request store)

Backs IdempotencyService: one row per (principal_id, scope, idempotency_key)
caches a handler's serialized response so a replayed/copied request returns the
stored answer instead of re-running an LLM call, re-sending an email, or
creating a duplicate row. The unique constraint also serializes concurrent twins.

Revision ID: fb2c3d4e5f6a
Revises: fa1b2c3d4e5f
Create Date: 2026-06-29 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fb2c3d4e5f6a"
down_revision: str | None = "fa1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_keys")),
        sa.UniqueConstraint(
            "principal_id",
            "scope",
            "idempotency_key",
            name="uq_idempotency_keys_principal_scope_key",
        ),
    )
    op.create_index(
        op.f("ix_idempotency_keys_principal_id"),
        "idempotency_keys",
        ["principal_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_idempotency_keys_principal_id"), table_name="idempotency_keys")
    op.drop_table("idempotency_keys")

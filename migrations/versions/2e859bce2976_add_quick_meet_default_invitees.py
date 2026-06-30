"""add quick meet default invitees

Revision ID: 2e859bce2976
Revises: f1a2b3c4d5e6
Create Date: 2026-06-22 11:52:34.202049
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2e859bce2976"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quick_meet_defaults",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("invitee_emails", sa.JSON(), nullable=False),
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
            ["owner_id"],
            ["employees.id"],
            name=op.f("fk_quick_meet_defaults_owner_id_employees"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quick_meet_defaults")),
        sa.UniqueConstraint("owner_id", name="uq_quick_meet_defaults_owner"),
    )
    op.create_index(
        op.f("ix_quick_meet_defaults_owner_id"),
        "quick_meet_defaults",
        ["owner_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_quick_meet_defaults_owner_id"), table_name="quick_meet_defaults")
    op.drop_table("quick_meet_defaults")

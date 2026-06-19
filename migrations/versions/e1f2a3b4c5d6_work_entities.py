"""work_entities table (attribution catalog)

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-06-17 16:55:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "work_entities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("department", sa.String(length=128), nullable=True),
        sa.Column("description", sa.String(length=1024), nullable=True),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("domains", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["employees.id"],
            name=op.f("fk_work_entities_created_by_employees"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["employees.id"],
            name=op.f("fk_work_entities_updated_by_employees"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_entities")),
    )
    op.create_index(op.f("ix_work_entities_department"), "work_entities", ["department"])
    op.create_index(op.f("ix_work_entities_is_active"), "work_entities", ["is_active"])


def downgrade() -> None:
    op.drop_index(op.f("ix_work_entities_is_active"), table_name="work_entities")
    op.drop_index(op.f("ix_work_entities_department"), table_name="work_entities")
    op.drop_table("work_entities")

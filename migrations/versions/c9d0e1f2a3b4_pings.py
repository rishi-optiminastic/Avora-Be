"""pings table

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-06-16 15:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_employee_id", sa.Uuid(), nullable=False),
        sa.Column("issued_by_id", sa.Uuid(), nullable=False),
        sa.Column("message", sa.String(length=280), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["target_employee_id"],
            ["employees.id"],
            name=op.f("fk_pings_target_employee_id_employees"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_id"],
            ["employees.id"],
            name=op.f("fk_pings_issued_by_id_employees"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pings")),
    )
    op.create_index(op.f("ix_pings_target_employee_id"), "pings", ["target_employee_id"])
    op.create_index(op.f("ix_pings_created_at"), "pings", ["created_at"])
    op.create_index("ix_pings_target_delivered", "pings", ["target_employee_id", "delivered_at"])


def downgrade() -> None:
    op.drop_index("ix_pings_target_delivered", table_name="pings")
    op.drop_index(op.f("ix_pings_created_at"), table_name="pings")
    op.drop_index(op.f("ix_pings_target_employee_id"), table_name="pings")
    op.drop_table("pings")

"""leave_allocations (HR/Admin per-employee leave-quota override, isolated table)

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-07-02 10:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "leave_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("planned_days", sa.Integer(), nullable=True),
        sa.Column("sick_days", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
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
            name=op.f("fk_leave_allocations_employee_id_employees"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["employees.id"],
            name=op.f("fk_leave_allocations_updated_by_employees"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave_allocations")),
        sa.UniqueConstraint("employee_id", name="uq_leave_allocations_employee_id"),
    )
    op.create_index(
        op.f("ix_leave_allocations_employee_id"), "leave_allocations", ["employee_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_leave_allocations_employee_id"), table_name="leave_allocations")
    op.drop_table("leave_allocations")

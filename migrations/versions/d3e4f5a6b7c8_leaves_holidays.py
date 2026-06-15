"""leaves + holidays tables

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-06-15 12:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "leaves",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column(
            "leave_type",
            sa.Enum("PLANNED", "SICK", "UNPAID", "HALF_DAY", name="leavetype"),
            nullable=False,
        ),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "SUBMITTED",
                "APPROVED",
                "REJECTED",
                "CANCELLED",
                "WITHDRAWN",
                name="leavestatus",
            ),
            nullable=False,
        ),
        sa.Column("reviewer_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(length=1000), nullable=True),
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
            name=op.f("fk_leaves_employee_id_employees"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_id"],
            ["employees.id"],
            name=op.f("fk_leaves_reviewer_id_employees"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leaves")),
    )
    op.create_index(op.f("ix_leaves_employee_id"), "leaves", ["employee_id"], unique=False)
    op.create_index(op.f("ix_leaves_leave_type"), "leaves", ["leave_type"], unique=False)
    op.create_index(op.f("ix_leaves_status"), "leaves", ["status"], unique=False)
    op.create_index("ix_leaves_employee_status", "leaves", ["employee_id", "status"], unique=False)

    op.create_table(
        "holidays",
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "holiday_type",
            sa.Enum("PUBLIC", "OPTIONAL", "COMPANY", name="holidaytype"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
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
            ["created_by"],
            ["employees.id"],
            name=op.f("fk_holidays_created_by_employees"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["employees.id"],
            name=op.f("fk_holidays_updated_by_employees"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_holidays")),
    )
    op.create_index(op.f("ix_holidays_date"), "holidays", ["date"], unique=False)
    op.create_index(op.f("ix_holidays_holiday_type"), "holidays", ["holiday_type"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_holidays_holiday_type"), table_name="holidays")
    op.drop_index(op.f("ix_holidays_date"), table_name="holidays")
    op.drop_table("holidays")
    op.drop_index("ix_leaves_employee_status", table_name="leaves")
    op.drop_index(op.f("ix_leaves_status"), table_name="leaves")
    op.drop_index(op.f("ix_leaves_leave_type"), table_name="leaves")
    op.drop_index(op.f("ix_leaves_employee_id"), table_name="leaves")
    op.drop_table("leaves")
    sa.Enum(name="holidaytype").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="leavestatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="leavetype").drop(op.get_bind(), checkfirst=True)

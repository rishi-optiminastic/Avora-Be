"""payroll_adjustments table (HR/Admin manual earnings/deductions/overrides)

Revision ID: b4d6f8a0c2e5
Revises: a3c5e7f9b2d4
Create Date: 2026-07-27 14:45:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4d6f8a0c2e5"
down_revision: str | Sequence[str] | None = "a3c5e7f9b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    kind = sa.Enum("EARNING", "DEDUCTION", "OVERRIDE", name="payrolladjustmentkind")
    target = sa.Enum(
        "BASIC",
        "HRA",
        "SPECIAL_ALLOWANCE",
        "EMPLOYEE_PF",
        "LOP_DAYS",
        "NET_PAY",
        name="payrolladjustmenttarget",
    )
    kind.create(op.get_bind(), checkfirst=True)
    target.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "payroll_adjustments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("period_month", sa.String(length=7), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(name="payrolladjustmentkind", create_type=False),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "target",
            postgresql.ENUM(name="payrolladjustmenttarget", create_type=False),
            nullable=True,
        ),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_payroll_adjustments_employee_id", "payroll_adjustments", ["employee_id"]
    )
    op.create_index(
        "ix_payroll_adjustments_period_month", "payroll_adjustments", ["period_month"]
    )
    op.create_index(
        "ix_payroll_adjustments_employee_period",
        "payroll_adjustments",
        ["employee_id", "period_month"],
    )


def downgrade() -> None:
    op.drop_table("payroll_adjustments")
    sa.Enum(name="payrolladjustmenttarget").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="payrolladjustmentkind").drop(op.get_bind(), checkfirst=True)

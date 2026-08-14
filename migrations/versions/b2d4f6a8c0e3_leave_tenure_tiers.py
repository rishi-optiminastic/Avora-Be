"""leave tenure tiers: leave_tier_quotas + leave_policy.probation_months

Revision ID: b2d4f6a8c0e3
Revises: e7f9a1c3b5d8
Create Date: 2026-08-14 08:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d4f6a8c0e3"
down_revision: str | Sequence[str] | None = "e7f9a1c3b5d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bands are derived from hire_date, never stored on the employee — this enum only
# labels the quota rows.
_TENURE = sa.Enum("probation", "confirmed", "tenured", name="tenurestatus")


def upgrade() -> None:
    op.add_column(
        "leave_policy",
        sa.Column("probation_months", sa.Integer(), nullable=False, server_default="6"),
    )

    _TENURE.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "leave_tier_quotas",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tier", _TENURE, nullable=False),
        sa.Column(
            "leave_type",
            sa.Enum(
                "planned",
                "annual",
                "sick",
                "bereavement",
                "birthday",
                "maternity",
                "paternity",
                "marriage",
                "unpaid",
                "half_day",
                name="leavetype",
                create_type=False,
            ),
            nullable=False,
        ),
        # NULL means "inherit the org LeavePolicy"; 0 means "this band gets none".
        sa.Column("annual_days", sa.Integer(), nullable=True),
        sa.Column("monthly_accrual_days", sa.Numeric(4, 2), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["updated_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tier", "leave_type", name="uq_leave_tier_quotas_tier_type"),
    )
    op.create_index("ix_leave_tier_quotas_tier", "leave_tier_quotas", ["tier"])
    op.create_index("ix_leave_tier_quotas_leave_type", "leave_tier_quotas", ["leave_type"])


def downgrade() -> None:
    op.drop_index("ix_leave_tier_quotas_leave_type", table_name="leave_tier_quotas")
    op.drop_index("ix_leave_tier_quotas_tier", table_name="leave_tier_quotas")
    op.drop_table("leave_tier_quotas")
    _TENURE.drop(op.get_bind(), checkfirst=True)
    op.drop_column("leave_policy", "probation_months")

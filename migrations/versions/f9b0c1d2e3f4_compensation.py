"""compensation (HR/Admin/self-scoped employee pay, isolated table)

Revision ID: f9b0c1d2e3f4
Revises: e3f4a5b6c7d8
Create Date: 2026-06-20 16:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f9b0c1d2e3f4"
down_revision: str | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sa.Enum("ANNUAL", "MONTHLY", name="payperiod").create(op.get_bind(), checkfirst=True)
    period = postgresql.ENUM(name="payperiod", create_type=False)
    op.create_table(
        "compensations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bonus_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("period", period, nullable=False, server_default="ANNUAL"),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_id", name="uq_compensations_employee_id"),
    )
    op.create_index("ix_compensations_employee_id", "compensations", ["employee_id"])


def downgrade() -> None:
    op.drop_index("ix_compensations_employee_id", table_name="compensations")
    op.drop_table("compensations")
    sa.Enum(name="payperiod").drop(op.get_bind(), checkfirst=True)

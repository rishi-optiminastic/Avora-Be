"""payrolladjustmenttarget: add present_days, paid_leave_days

Lets an OVERRIDE edit the present-days and paid-leave-days counts directly on the
payroll card (they feed the loss-of-pay / payable-days computation).

Revision ID: c4e6a8b0d2f5
Revises: d3e5f7a9c1b4
Create Date: 2026-08-01 12:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c4e6a8b0d2f5"
down_revision: str | Sequence[str] | None = "d3e5f7a9c1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Stored values are the enum member NAMES (uppercase), matching BASIC/HRA/...
    for value in ("PRESENT_DAYS", "PAID_LEAVE_DAYS"):
        op.execute(f"ALTER TYPE payrolladjustmenttarget ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; leaving it is harmless.
    pass

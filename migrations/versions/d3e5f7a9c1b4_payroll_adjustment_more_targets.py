"""payrolladjustmenttarget: add payable_days, professional_tax, income_tax

Lets an OVERRIDE edit paid (payable) days directly, plus the two computed
deductions — so HR/finance can correct essentially every field on a slip.

Revision ID: d3e5f7a9c1b4
Revises: a9c1e3f5b7d2
Create Date: 2026-07-31 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d3e5f7a9c1b4"
down_revision: str | Sequence[str] | None = "a9c1e3f5b7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Stored values are the enum member NAMES (uppercase), matching the existing
    # BASIC/HRA/... members.
    for value in ("PROFESSIONAL_TAX", "INCOME_TAX", "PAYABLE_DAYS"):
        op.execute(f"ALTER TYPE payrolladjustmenttarget ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; leaving it is harmless.
    pass

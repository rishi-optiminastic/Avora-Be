"""payroll_settings: February professional-tax amount + income-tax toggle

Revision ID: d7e8f9a0b1c2
Revises: f0a1b2c3d4e5
Create Date: 2026-07-02

Professional tax commonly steps up in February and is flat the other eleven
months; `professional_tax_minor` already covers "the other months", this adds
the February amount alongside it. `deduct_income_tax` lets HR turn off the new
monthly TDS estimate (`app/core/payroll.py`) without touching PF/PT.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7e8f9a0b1c2"
down_revision: str | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payroll_settings",
        sa.Column(
            "professional_tax_feb_minor", sa.BigInteger(), nullable=False, server_default="30000"
        ),
    )
    op.add_column(
        "payroll_settings",
        sa.Column(
            "deduct_income_tax", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade() -> None:
    op.drop_column("payroll_settings", "deduct_income_tax")
    op.drop_column("payroll_settings", "professional_tax_feb_minor")

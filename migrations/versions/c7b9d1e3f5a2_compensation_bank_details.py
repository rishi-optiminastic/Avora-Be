"""compensation bank details (holder, bank, encrypted account no, IFSC, type)

Revision ID: c7b9d1e3f5a2
Revises: ab533da8efd6
Create Date: 2026-07-22

Adds salary-disbursal bank details to the isolated `compensations` table (same
access control as pay). The account number is stored as Fernet ciphertext, so
the column holds encrypted text; the rest are low-risk plaintext.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7b9d1e3f5a2"
down_revision: str | None = "ab533da8efd6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    account_type = sa.Enum("SAVINGS", "CURRENT", name="accounttype")
    account_type.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "compensations",
        sa.Column("account_holder_name", sa.String(length=128), nullable=True),
    )
    op.add_column("compensations", sa.Column("bank_name", sa.String(length=128), nullable=True))
    op.add_column(
        "compensations",
        sa.Column("account_number_encrypted", sa.String(length=512), nullable=True),
    )
    op.add_column("compensations", sa.Column("ifsc_code", sa.String(length=16), nullable=True))
    op.add_column(
        "compensations",
        sa.Column("account_type", account_type, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("compensations", "account_type")
    op.drop_column("compensations", "ifsc_code")
    op.drop_column("compensations", "account_number_encrypted")
    op.drop_column("compensations", "bank_name")
    op.drop_column("compensations", "account_holder_name")
    sa.Enum(name="accounttype").drop(op.get_bind(), checkfirst=True)

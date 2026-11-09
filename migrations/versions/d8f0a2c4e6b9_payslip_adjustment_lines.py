"""Itemise manual adjustments on a released payslip.

A Diwali bonus or an advance recovery was already inside a slip's net, but
nothing on the slip said so: the Earnings and Deductions ledgers are built from
`prorated_breakdown`, which carries only the formula-derived components, so Net
no longer equalled Gross minus Deductions and the person could not see what the
extra (or missing) money was. This column freezes each manual earning and
deduction by name - [{kind, label, amount_minor}] - next to the snapshot.
Defaults to an empty list, so slips released before today render as they did.

Revision ID: d8f0a2c4e6b9
Revises: c4e6a8b0d2f4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d8f0a2c4e6b9"
down_revision = "c4e6a8b0d2f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payslips",
        sa.Column("adjustments", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    op.drop_column("payslips", "adjustments")

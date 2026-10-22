"""Withdrawal request on an approved leave

Taking back granted time off is now a request the reporting manager decides,
not something the employee does alone. The leave keeps its own `status` - it
stays APPROVED, and so stays counted by the balance and payroll - until the
withdrawal is granted, at which point it becomes CANCELLED like any other
cancellation.

`leavewithdrawalstatus` is a NEW enum type rather than extra members on
`leavestatus`: these are two independent axes, and merging them would have made
"approved" ambiguous in every existing query.

Revision ID: c5e7a9b1d3f6
Revises: b4d6f8a0c2e4
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e7a9b1d3f6"
down_revision: str | None = "b4d6f8a0c2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUM_NAME = "leavewithdrawalstatus"
# SQLAlchemy stores the StrEnum MEMBER NAMES, so the labels are uppercase.
_MEMBERS = ("NONE", "REQUESTED", "APPROVED", "REJECTED")


def upgrade() -> None:
    status = sa.Enum(*_MEMBERS, name=_ENUM_NAME)
    status.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "leaves",
        sa.Column(
            "withdrawal_status",
            status,
            nullable=False,
            server_default="NONE",
        ),
    )
    op.add_column("leaves", sa.Column("withdrawal_reason", sa.String(length=1000), nullable=True))
    op.add_column(
        "leaves",
        sa.Column("withdrawal_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("leaves", sa.Column("withdrawal_reviewer_id", sa.Uuid(), nullable=True))
    op.add_column(
        "leaves", sa.Column("withdrawal_decided_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "leaves", sa.Column("withdrawal_decision_note", sa.String(length=1000), nullable=True)
    )
    op.create_foreign_key(
        "fk_leaves_withdrawal_reviewer_id_employees",
        "leaves",
        "employees",
        ["withdrawal_reviewer_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Indexed because the manager's queue filters on it, and almost every row is
    # NONE - exactly the shape a btree index on a skewed column handles well.
    op.create_index("ix_leaves_withdrawal_status", "leaves", ["withdrawal_status"])


def downgrade() -> None:
    op.drop_index("ix_leaves_withdrawal_status", table_name="leaves")
    op.drop_constraint("fk_leaves_withdrawal_reviewer_id_employees", "leaves", type_="foreignkey")
    for column in (
        "withdrawal_decision_note",
        "withdrawal_decided_at",
        "withdrawal_reviewer_id",
        "withdrawal_requested_at",
        "withdrawal_reason",
        "withdrawal_status",
    ):
        op.drop_column("leaves", column)
    sa.Enum(name=_ENUM_NAME).drop(op.get_bind(), checkfirst=True)

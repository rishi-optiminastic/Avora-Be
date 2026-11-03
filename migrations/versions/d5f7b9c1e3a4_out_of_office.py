"""out_of_office — days worked away from the office, approved by the manager

A day spent at the printing warehouse or out collecting payment is worked, but
the office terminal never sees it. Until now such a day scored as absent and the
only remedy was an HR override forcing "full day", which recorded neither where
the person went nor who sent them.

Deliberately its own table rather than a leave type or a regularization: no quota
is spent (they were working) and no monthly credit is burned (nothing is being
corrected). See app/models/out_of_office.py.

Revision ID: d5f7b9c1e3a4
Revises: c3e5a7b9d1f2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d5f7b9c1e3a4"
down_revision = "c3e5a7b9d1f2"
branch_labels = None
depends_on = None

_PORTION = sa.Enum("FULL_DAY", "HALF_DAY", name="outofofficeportion")
_STATUS = sa.Enum("PENDING", "APPROVED", "REJECTED", "CANCELLED", name="outofofficestatus")


def upgrade() -> None:
    bind = op.get_bind()
    # Create the enums up front and tell create_table not to re-create them:
    # with several workspace schemas on one database, letting the column
    # definition emit its own CREATE TYPE races itself inside one transaction.
    _PORTION.create(bind, checkfirst=True)
    _STATUS.create(bind, checkfirst=True)

    op.create_table(
        "out_of_office",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "employee_id",
            sa.UUID(),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Local office dates, like attendance_overrides: attendance is reckoned
        # in office-local days, so an instant would need converting at every read.
        sa.Column("start_day", sa.String(length=10), nullable=False),
        sa.Column("end_day", sa.String(length=10), nullable=False),
        sa.Column(
            "portion",
            _PORTION.copy(),  # type: ignore[attr-defined]
            nullable=False,
            server_default="FULL_DAY",
        ),
        sa.Column("location", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column(
            "status",
            _STATUS.copy(),  # type: ignore[attr-defined]
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "reviewed_by_id",
            sa.UUID(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_out_of_office_employee_id", "out_of_office", ["employee_id"])
    op.create_index("ix_out_of_office_status", "out_of_office", ["status"])
    op.create_index("ix_out_of_office_reviewed_by_id", "out_of_office", ["reviewed_by_id"])
    op.create_index("ix_out_of_office_employee_status", "out_of_office", ["employee_id", "status"])
    op.create_index(
        "ix_out_of_office_employee_days",
        "out_of_office",
        ["employee_id", "start_day", "end_day"],
    )


def downgrade() -> None:
    op.drop_table("out_of_office")
    bind = op.get_bind()
    _STATUS.drop(bind, checkfirst=True)
    _PORTION.drop(bind, checkfirst=True)

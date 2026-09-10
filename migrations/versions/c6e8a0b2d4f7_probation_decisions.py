"""The outcome of a probation review, and whether its letter was sent.

Labels are UPPERCASE (SQLAlchemy persists an Enum by MEMBER NAME) and the column
uses postgresql.ENUM with create_type=False so `create_table` does not re-emit
CREATE TYPE after the explicit create. sa.Enum silently ignores that flag and the
migration dies with `type already exists`; verified against a real Postgres.

Revision ID: c6e8a0b2d4f7
Revises: b5d7f9a1c3e6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c6e8a0b2d4f7"
down_revision = "b5d7f9a1c3e6"
branch_labels = None
depends_on = None

_TABLE = "probation_decisions"
_ENUM = "probationoutcome"
_LABELS = ("CONFIRMED", "EXTENDED", "TERMINATED")


def upgrade() -> None:
    outcome = postgresql.ENUM(*_LABELS, name=_ENUM, create_type=False)
    outcome.create(op.get_bind(), checkfirst=True)
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", outcome, nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("job_title", sa.String(length=128), nullable=True),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("letter_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name="fk_probation_decisions_employee_id_employees",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["employees.id"],
            name="fk_probation_decisions_decided_by_employees",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_probation_decisions"),
        sa.UniqueConstraint("employee_id", name="uq_probation_decisions_employee_id"),
    )
    op.create_index("ix_probation_decisions_employee_id", _TABLE, ["employee_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_probation_decisions_employee_id", table_name=_TABLE)
    op.drop_table(_TABLE)
    postgresql.ENUM(name=_ENUM, create_type=False).drop(op.get_bind(), checkfirst=True)

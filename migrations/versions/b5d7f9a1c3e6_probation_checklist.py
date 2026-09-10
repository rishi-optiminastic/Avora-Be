"""Per-employee progress through the probation review checklist.

Only steps someone has acted on get a row, so there is no backfill: an employee
with no rows simply has nothing ticked yet.

The status enum labels are UPPERCASE because SQLAlchemy persists an Enum column
by MEMBER NAME, not value. Lowercase labels here would produce a type the ORM can
never write to, and it would only fail against real Postgres — the same trap that
took down task comments and the leave pages before.

Revision ID: b5d7f9a1c3e6
Revises: a4c6e8b0d2f5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b5d7f9a1c3e6"
down_revision = "a4c6e8b0d2f5"
branch_labels = None
depends_on = None

_TABLE = "probation_checklist_items"
_ENUM = "probationstepstatus"
_LABELS = ("PENDING", "DONE", "SKIPPED")


def upgrade() -> None:
    # create_type=False keeps create_table from re-emitting CREATE TYPE below.
    status = postgresql.ENUM(*_LABELS, name=_ENUM, create_type=False)
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("status", status, nullable=False, server_default="PENDING"),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name="fk_probation_checklist_items_employee_id_employees",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["employees.id"],
            name="fk_probation_checklist_items_actor_id_employees",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_probation_checklist_items"),
        sa.UniqueConstraint("employee_id", "step_no", name="uq_probation_item_employee_step"),
    )
    op.create_index(
        "ix_probation_checklist_items_employee_id", _TABLE, ["employee_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_probation_checklist_items_employee_id", table_name=_TABLE)
    op.drop_table(_TABLE)
    postgresql.ENUM(name=_ENUM, create_type=False).drop(op.get_bind(), checkfirst=True)

"""Sheets: share with named people (viewer or editor), plus a notification kind.

`workbooks.sharing` stays as the general access level; `workbook_members` adds
people by name on top of it. Both enums store member NAMES like every other
native enum here. ADD VALUE on `notificationkind` is safe inside the
transaction because nothing uses the new label in it.

Revision ID: f2c4e6a8b0d3
Revises: e1b3d5f7a9c2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f2c4e6a8b0d3"
down_revision = "e1b3d5f7a9c2"
branch_labels = None
depends_on = None

_ENUM = "workbookaccess"
_TABLE = "workbook_members"


def upgrade() -> None:
    op.execute("ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS 'SHEET_SHARED'")
    sa.Enum("VIEW", "EDIT", name=_ENUM).create(op.get_bind(), checkfirst=True)
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workbook_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column(
            "access",
            postgresql.ENUM(name=_ENUM, create_type=False),
            nullable=False,
            server_default="VIEW",
        ),
        sa.Column("added_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workbook_id"],
            ["workbooks.id"],
            name="fk_workbook_members_workbook_id_workbooks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name="fk_workbook_members_employee_id_employees",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["added_by_id"],
            ["employees.id"],
            name="fk_workbook_members_added_by_id_employees",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workbook_members"),
        sa.UniqueConstraint("workbook_id", "employee_id", name="uq_workbook_members_workbook_id"),
    )
    op.create_index("ix_workbook_members_workbook_id", _TABLE, ["workbook_id"])
    op.create_index("ix_workbook_members_employee_id", _TABLE, ["employee_id"])


def downgrade() -> None:
    op.drop_index("ix_workbook_members_employee_id", table_name=_TABLE)
    op.drop_index("ix_workbook_members_workbook_id", table_name=_TABLE)
    op.drop_table(_TABLE)
    sa.Enum(name=_ENUM).drop(op.get_bind(), checkfirst=True)
    # The extra notification label is harmless; Postgres cannot drop one value.

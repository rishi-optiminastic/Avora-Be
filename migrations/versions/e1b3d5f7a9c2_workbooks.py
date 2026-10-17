"""Sheets: spreadsheets kept in Avora.

One row per workbook. `snapshot` is the editor's whole document as JSON (JSONB
in Postgres); `version` backs the optimistic lock that stops one person's save
silently overwriting another's. `sharing` is a native enum storing member NAMES,
like every other enum column here.

Revision ID: e1b3d5f7a9c2
Revises: d9a1c3e5f7b2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e1b3d5f7a9c2"
down_revision = "d9a1c3e5f7b2"
branch_labels = None
depends_on = None

_ENUM = "workbooksharing"


def upgrade() -> None:
    bind = op.get_bind()
    sa.Enum("PRIVATE", "ORG_VIEW", "ORG_EDIT", name=_ENUM).create(bind, checkfirst=True)
    op.create_table(
        "workbooks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column(
            "sharing",
            postgresql.ENUM(name=_ENUM, create_type=False),
            nullable=False,
            server_default="PRIVATE",
        ),
        sa.Column(
            "snapshot",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["employees.id"], name="fk_workbooks_owner_id_employees"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["employees.id"],
            name="fk_workbooks_updated_by_id_employees",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workbooks"),
    )
    op.create_index("ix_workbooks_owner_id", "workbooks", ["owner_id"])
    op.create_index("ix_workbooks_sharing_updated", "workbooks", ["sharing", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_workbooks_sharing_updated", table_name="workbooks")
    op.drop_index("ix_workbooks_owner_id", table_name="workbooks")
    op.drop_table("workbooks")
    sa.Enum(name=_ENUM).drop(op.get_bind(), checkfirst=True)

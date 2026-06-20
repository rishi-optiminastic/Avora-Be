"""attribution_corrections table (manual label correction + review)

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-06-19 15:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b0c1d2e3f4a5"
down_revision: str | None = "a9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    status = sa.Enum("PENDING", "APPROVED", "REJECTED", name="correctionstatus")
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "attribution_corrections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("proposed_by_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("screenshot_id", sa.Uuid(), nullable=True),
        sa.Column("for_date", sa.String(length=10), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="correctionstatus", create_type=False),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("reviewed_by_id", sa.Uuid(), nullable=True),
        sa.Column("review_note", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["proposed_by_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["work_entities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["screenshot_id"], ["screenshots.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_attribution_corrections_employee_id", "attribution_corrections", ["employee_id"]
    )
    op.create_index(
        "ix_attr_corrections_employee_status",
        "attribution_corrections",
        ["employee_id", "status"],
    )
    op.create_index("ix_attribution_corrections_status", "attribution_corrections", ["status"])


def downgrade() -> None:
    op.drop_index("ix_attribution_corrections_status", table_name="attribution_corrections")
    op.drop_index("ix_attr_corrections_employee_status", table_name="attribution_corrections")
    op.drop_index("ix_attribution_corrections_employee_id", table_name="attribution_corrections")
    op.drop_table("attribution_corrections")
    sa.Enum(name="correctionstatus").drop(op.get_bind(), checkfirst=True)

"""resignations + festivals + celebration_settings, and resignation notif kinds

Revision ID: d1e2f3a4b5c7
Revises: c9e1f2a3b4d5
Create Date: 2026-07-10 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d1e2f3a4b5c7"
down_revision: str | None = "c9e1f2a3b4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- resignations ------------------------------------------------------ #
    status = sa.Enum(
        "SUBMITTED", "ACCEPTED", "REJECTED", "WITHDRAWN", name="resignationstatus"
    )
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "resignations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=True),
        sa.Column("last_working_day", sa.Date(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="resignationstatus", create_type=False),
            nullable=False,
            server_default="SUBMITTED",
        ),
        sa.Column("reviewer_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewer_id"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_resignations_employee_id", "resignations", ["employee_id"])
    op.create_index("ix_resignations_status", "resignations", ["status"])
    op.create_index("ix_resignations_reviewer_id", "resignations", ["reviewer_id"])
    op.create_index(
        "ix_resignations_employee_status", "resignations", ["employee_id", "status"]
    )

    # --- festivals --------------------------------------------------------- #
    op.create_table(
        "festivals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("on_date", sa.Date(), nullable=False),
        sa.Column("message", sa.String(length=2000), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_festivals_on_date", "festivals", ["on_date"])

    # --- celebration_settings (singleton) ---------------------------------- #
    op.create_table(
        "celebration_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column(
            "birthday_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "anniversary_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "festival_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("last_run_on", sa.Date(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["updated_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    # --- notification kinds for resignation -------------------------------- #
    for value in ("RESIGNATION_SUBMITTED", "RESIGNATION_DECISION"):
        op.execute(f"ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    op.drop_table("celebration_settings")
    op.drop_index("ix_festivals_on_date", table_name="festivals")
    op.drop_table("festivals")
    for name in (
        "ix_resignations_employee_status",
        "ix_resignations_reviewer_id",
        "ix_resignations_status",
        "ix_resignations_employee_id",
    ):
        op.drop_index(name, table_name="resignations")
    op.drop_table("resignations")
    sa.Enum(name="resignationstatus").drop(op.get_bind(), checkfirst=True)
    # PostgreSQL can't drop enum values; the added notificationkind values remain.

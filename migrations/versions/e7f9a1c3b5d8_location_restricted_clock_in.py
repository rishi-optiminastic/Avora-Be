"""location-restricted clock-in: office_locations + policy toggle + exempt + coords

Revision ID: e7f9a1c3b5d8
Revises: c4e6a8b0d2f5
Create Date: 2026-08-03 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7f9a1c3b5d8"
down_revision: str | Sequence[str] | None = "c4e6a8b0d2f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "office_locations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("radius_m", sa.Integer(), nullable=False, server_default="150"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_office_locations_is_active", "office_locations", ["is_active"])

    op.add_column(
        "attendance_policy",
        sa.Column(
            "require_location_for_clock_in", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "employees",
        sa.Column(
            "location_check_exempt", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("work_sessions", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("work_sessions", sa.Column("longitude", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("work_sessions", "longitude")
    op.drop_column("work_sessions", "latitude")
    op.drop_column("employees", "location_check_exempt")
    op.drop_column("attendance_policy", "require_location_for_clock_in")
    op.drop_index("ix_office_locations_is_active", table_name="office_locations")
    op.drop_table("office_locations")

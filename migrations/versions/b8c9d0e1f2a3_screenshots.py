"""screenshots table

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-06-16 14:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screenshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_screenshots_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_screenshots_employee_id_employees"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screenshots")),
    )
    op.create_index(op.f("ix_screenshots_device_id"), "screenshots", ["device_id"])
    op.create_index(op.f("ix_screenshots_employee_id"), "screenshots", ["employee_id"])
    op.create_index(op.f("ix_screenshots_received_at"), "screenshots", ["received_at"])
    op.create_index(
        "ix_screenshots_employee_received", "screenshots", ["employee_id", "received_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_screenshots_employee_received", table_name="screenshots")
    op.drop_index(op.f("ix_screenshots_received_at"), table_name="screenshots")
    op.drop_index(op.f("ix_screenshots_employee_id"), table_name="screenshots")
    op.drop_index(op.f("ix_screenshots_device_id"), table_name="screenshots")
    op.drop_table("screenshots")

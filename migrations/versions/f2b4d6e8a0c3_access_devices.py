"""Access - track physical devices (laptops, monitors, keyboards, ID cards).

A device is a tech asset of kind DEVICE, assigned to a person through the same
grant table. `details.ownership` records whether the company owns it or the
person brought their own (BYOD); Avora refuses to store a password for a
personal device.

ADD VALUE runs in its own migration: Postgres will not let a transaction use an
enum value it added, and nothing here uses it.

Revision ID: f2b4d6e8a0c3
Revises: e1a3c5f7b9d2
"""

from __future__ import annotations

from alembic import op

revision = "f2b4d6e8a0c3"
down_revision = "e1a3c5f7b9d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE techassetkind ADD VALUE IF NOT EXISTS 'DEVICE'")


def downgrade() -> None:
    # Postgres cannot drop one enum value without rebuilding the type; an unused
    # 'DEVICE' label is harmless.
    pass

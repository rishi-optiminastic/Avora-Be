"""Probation review forms: three new form triggers, a manager-missing state and
weighted rating questions.

- `formtrigger` gains PROBATION_SELF, PROBATION_MANAGER, PROBATION_HR.
- `assignmentstatus` gains NEEDS_MANAGER (a manager form waiting for the
  probationer to have a reporting manager on file).
- `form_fields.weight`: a rating question's percent share of a 100-point score.

Enum labels are UPPERCASE member names (see tests/test_migration_enum_labels.py).
ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older
Postgres, so the labels are added in an autocommit block; IF NOT EXISTS makes it
idempotent.

Revision ID: b7e9a1c3d5f8
Revises: a4c6e8f0b2d5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7e9a1c3d5f8"
down_revision = "a4c6e8f0b2d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("form_fields", sa.Column("weight", sa.Integer(), nullable=True))
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE formtrigger ADD VALUE IF NOT EXISTS 'PROBATION_SELF'")
        op.execute("ALTER TYPE formtrigger ADD VALUE IF NOT EXISTS 'PROBATION_MANAGER'")
        op.execute("ALTER TYPE formtrigger ADD VALUE IF NOT EXISTS 'PROBATION_HR'")
        op.execute("ALTER TYPE assignmentstatus ADD VALUE IF NOT EXISTS 'NEEDS_MANAGER'")


def downgrade() -> None:
    # Postgres cannot drop a single enum label, and an unused one costs nothing.
    op.drop_column("form_fields", "weight")

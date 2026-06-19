"""screenshot image in S3 (object_key) + nullable image bytes

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-06-19 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New images live in S3 under object_key; the in-DB image column becomes the
    # optional legacy fallback (kept for pre-S3 rows).
    op.add_column("screenshots", sa.Column("object_key", sa.String(length=512), nullable=True))
    op.alter_column("screenshots", "image", existing_type=sa.LargeBinary(), nullable=True)


def downgrade() -> None:
    op.alter_column("screenshots", "image", existing_type=sa.LargeBinary(), nullable=False)
    op.drop_column("screenshots", "object_key")

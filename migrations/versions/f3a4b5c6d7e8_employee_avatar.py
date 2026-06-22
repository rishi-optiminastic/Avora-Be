"""employee avatar (profile photo — S3 key or DB-fallback bytes)

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-06-20 21:50:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: str | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("avatar_object_key", sa.String(length=512), nullable=True))
    op.add_column("employees", sa.Column("avatar_content", sa.LargeBinary(), nullable=True))
    op.add_column(
        "employees", sa.Column("avatar_content_type", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("employees", "avatar_content_type")
    op.drop_column("employees", "avatar_content")
    op.drop_column("employees", "avatar_object_key")

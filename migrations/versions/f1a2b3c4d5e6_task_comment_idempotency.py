"""task_comments idempotency key (replay-proof discussion posts)

Adds a nullable `idempotency_key` and a unique (author_id, idempotency_key)
constraint so a replayed/copied request can't create a duplicate comment.

Revision ID: f1a2b3c4d5e6
Revises: e9f0a1b2c3d4
Create Date: 2026-06-22 10:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_comments",
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
    )
    # NULLs are distinct in Postgres, so legacy/keyless rows are unaffected; only
    # two real keys from the same author collide — which is exactly the replay.
    op.create_unique_constraint(
        "uq_task_comments_author_idempotency",
        "task_comments",
        ["author_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_task_comments_author_idempotency", "task_comments", type_="unique")
    op.drop_column("task_comments", "idempotency_key")

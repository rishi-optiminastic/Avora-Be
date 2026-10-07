"""merge task-escalation + circle-documents heads

Revision ID: b2d4f6a8c0e1
Revises: f1a3c5e7b9d2, a4c6e8f0b2d1
Create Date: 2026-10-05

Two features branched from e8a0c2d4f6b9 independently - task escalation added
`tasks.escalation_level`, the Circle integration added its document-import
tables. Neither touches the other's tables, so this merge is a no-op join that
simply gives Alembic a single head again.

Without it `alembic upgrade head` fails outright ("Multiple head revisions are
present"), which takes the whole deploy down before any schema change is applied.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "b2d4f6a8c0e1"
down_revision: str | Sequence[str] | None = ("f1a3c5e7b9d2", "a4c6e8f0b2d1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

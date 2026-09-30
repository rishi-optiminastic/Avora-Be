"""Add REGULARIZATION_REQUEST to the notification kind enum.

A regularization request now notifies the reporting manager. The Python enum
gained the member, but a Postgres enum needs the label too — and without it the
notification insert fails, rolling back the surrounding transaction, so the
employee cannot file a regularization AT ALL. Tests build the schema with
`create_all` on SQLite, where enums are plain text, so nothing caught it before
production did.

UPPERCASE because SQLAlchemy persists an Enum column by MEMBER NAME.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older
Postgres, so this commits first; IF NOT EXISTS makes it idempotent.

Revision ID: e8a0c2d4f6b9
Revises: d7f9b1c3e5a8
"""

from __future__ import annotations

from alembic import op

revision = "e8a0c2d4f6b9"
down_revision = "d7f9b1c3e5a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute("ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS 'REGULARIZATION_REQUEST'")


def downgrade() -> None:
    # Postgres cannot drop a single enum label, and an unused one costs nothing.
    pass

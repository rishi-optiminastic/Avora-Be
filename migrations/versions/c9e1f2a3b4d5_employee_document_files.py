"""employee documents: support uploaded files (not just links)

Makes `employee_documents.url` nullable (a document is now either a link OR an
uploaded file) and adds the file-storage columns (content_type / byte_size /
original_filename / object_key / content), mirroring workspace files.

Revision ID: c9e1f2a3b4d5
Revises: b8c1d2e3f4a5
Create Date: 2026-07-10 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e1f2a3b4d5"
down_revision: str | None = "b8c1d2e3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("employee_documents", "url", existing_type=sa.String(2048), nullable=True)
    op.add_column(
        "employee_documents", sa.Column("content_type", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "employee_documents",
        sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "employee_documents", sa.Column("original_filename", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "employee_documents", sa.Column("object_key", sa.String(length=512), nullable=True)
    )
    op.add_column("employee_documents", sa.Column("content", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("employee_documents", "content")
    op.drop_column("employee_documents", "object_key")
    op.drop_column("employee_documents", "original_filename")
    op.drop_column("employee_documents", "byte_size")
    op.drop_column("employee_documents", "content_type")
    # Existing link rows all have a url, so restoring NOT NULL is safe.
    op.alter_column("employee_documents", "url", existing_type=sa.String(2048), nullable=False)

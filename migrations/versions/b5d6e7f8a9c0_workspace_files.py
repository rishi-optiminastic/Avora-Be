"""workspace_files (shared team-drive files, org-readable, S3 or DB-fallback bytes)

Revision ID: b5d6e7f8a9c0
Revises: a1c2e3f4b5d6
Create Date: 2026-06-20 19:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5d6e7f8a9c0"
down_revision: str | None = "a1c2e3f4b5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sa.Enum(
        "DELIVERABLE",
        "BRIEF",
        "REFERENCE",
        "REPORT",
        "ASSET",
        "OTHER",
        name="workspacefilecategory",
    ).create(op.get_bind(), checkfirst=True)
    category = postgresql.ENUM(name="workspacefilecategory", create_type=False)
    op.create_table(
        "workspace_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("category", category, nullable=False, server_default="OTHER"),
        sa.Column(
            "content_type",
            sa.String(length=128),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("object_key", sa.String(length=512), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["work_entities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workspace_files_category", "workspace_files", ["category"])
    op.create_index("ix_workspace_files_project_id", "workspace_files", ["project_id"])
    op.create_index("ix_workspace_files_uploaded_by", "workspace_files", ["uploaded_by"])
    op.create_index(
        "ix_workspace_files_project_category", "workspace_files", ["project_id", "category"]
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_files_project_category", table_name="workspace_files")
    op.drop_index("ix_workspace_files_uploaded_by", table_name="workspace_files")
    op.drop_index("ix_workspace_files_project_id", table_name="workspace_files")
    op.drop_index("ix_workspace_files_category", table_name="workspace_files")
    op.drop_table("workspace_files")
    sa.Enum(name="workspacefilecategory").drop(op.get_bind(), checkfirst=True)

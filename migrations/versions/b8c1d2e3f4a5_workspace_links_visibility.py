"""workspace files: link url + per-entry visibility (department / individual)

Adds to `workspace_files`:
- `url` (nullable) — a link entry (Google Sheet/Doc, receipt, …) instead of bytes
- `visibility` (new `workspacevisibility` enum, default EVERYONE)
- `visible_departments` / `visible_employee_ids` (JSON, default []) — the ACL for
  a RESTRICTED entry

Revision ID: b8c1d2e3f4a5
Revises: d4e5f6a7b8c1
Create Date: 2026-07-10 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8c1d2e3f4a5"
down_revision: str | None = "d4e5f6a7b8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    visibility = sa.Enum("EVERYONE", "RESTRICTED", name="workspacevisibility")
    visibility.create(op.get_bind(), checkfirst=True)

    op.add_column("workspace_files", sa.Column("url", sa.String(length=2048), nullable=True))
    op.add_column(
        "workspace_files",
        sa.Column(
            "visibility",
            postgresql.ENUM("EVERYONE", "RESTRICTED", name="workspacevisibility", create_type=False),
            nullable=False,
            server_default="EVERYONE",
        ),
    )
    op.add_column(
        "workspace_files",
        sa.Column("visible_departments", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "workspace_files",
        sa.Column(
            "visible_employee_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")
        ),
    )
    op.create_index(
        "ix_workspace_files_visibility", "workspace_files", ["visibility"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_files_visibility", table_name="workspace_files")
    op.drop_column("workspace_files", "visible_employee_ids")
    op.drop_column("workspace_files", "visible_departments")
    op.drop_column("workspace_files", "visibility")
    op.drop_column("workspace_files", "url")
    sa.Enum(name="workspacevisibility").drop(op.get_bind(), checkfirst=True)

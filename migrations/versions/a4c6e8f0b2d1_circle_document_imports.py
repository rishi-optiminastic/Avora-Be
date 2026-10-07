"""circle document imports

Revision ID: a4c6e8f0b2d1
Revises: e8a0c2d4f6b9
Create Date: 2026-10-05

Adds `circle_document_imports`, the ledger that lets Avora copy each employee's
Circle documents exactly once. A new table only: no existing table changes, so
it is safe to apply ahead of the code that uses it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a4c6e8f0b2d1"
down_revision = "e8a0c2d4f6b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "circle_document_imports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "employee_id",
            sa.Uuid(),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("circle_document_id", sa.String(64), nullable=False),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("employee_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "employee_id", "circle_document_id", name="uq_circle_document_imports_employee_doc"
        ),
    )
    op.create_index(
        "ix_circle_document_imports_employee_id", "circle_document_imports", ["employee_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_circle_document_imports_employee_id", table_name="circle_document_imports")
    op.drop_table("circle_document_imports")

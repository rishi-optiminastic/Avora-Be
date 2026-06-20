"""employee_documents (HR/Admin/self-scoped registry of document links)

Revision ID: a1c2e3f4b5d6
Revises: f9b0c1d2e3f4
Create Date: 2026-06-20 18:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c2e3f4b5d6"
down_revision: str | None = "f9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sa.Enum(
        "IDENTITY",
        "CONTRACT",
        "PAYSLIP",
        "TAX",
        "CERTIFICATE",
        "OTHER",
        name="documentcategory",
    ).create(op.get_bind(), checkfirst=True)
    category = postgresql.ENUM(name="documentcategory", create_type=False)
    op.create_table(
        "employee_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("category", category, nullable=False, server_default="OTHER"),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["employees.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_employee_documents_employee_id", "employee_documents", ["employee_id"])


def downgrade() -> None:
    op.drop_index("ix_employee_documents_employee_id", table_name="employee_documents")
    op.drop_table("employee_documents")
    sa.Enum(name="documentcategory").drop(op.get_bind(), checkfirst=True)

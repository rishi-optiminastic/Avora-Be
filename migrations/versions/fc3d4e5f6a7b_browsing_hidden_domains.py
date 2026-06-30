"""browsing_hidden_domains table (personal per-employee domain hide list)

Backs BrowsingPrivacyService: one row per (employee_id, domain) the configured
owner has chosen to hide from the Browsing tab. The viewed employee's hidden set
is applied for every viewer, so the domain and its time never surface.

Revision ID: fc3d4e5f6a7b
Revises: fb2c3d4e5f6a
Create Date: 2026-06-30 09:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fc3d4e5f6a7b"
down_revision: str | None = "fb2c3d4e5f6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browsing_hidden_domains",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(length=256), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_browsing_hidden_domains_employee_id_employees"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browsing_hidden_domains")),
        sa.UniqueConstraint(
            "employee_id", "domain", name="uq_browsing_hidden_domains_employee_domain"
        ),
    )
    op.create_index(
        op.f("ix_browsing_hidden_domains_employee_id"),
        "browsing_hidden_domains",
        ["employee_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_browsing_hidden_domains_employee_id"),
        table_name="browsing_hidden_domains",
    )
    op.drop_table("browsing_hidden_domains")

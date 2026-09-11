"""Add POLICY to the workspace file category enum.

Company policies (leave, reimbursement, dress code) are published into the same
workspace store everything else uses; only the category is new. Everyone reads
them, but the service restricts publishing and withdrawing to HR/Admin.

The label is UPPERCASE because SQLAlchemy persists an Enum column by MEMBER NAME.
A lowercase 'policy' here would be a value the ORM can never write, and it would
only fail against real Postgres.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older Postgres,
so this commits first; it is idempotent via IF NOT EXISTS.

Revision ID: d7f9b1c3e5a8
Revises: c6e8a0b2d4f7
"""

from __future__ import annotations

from alembic import op

revision = "d7f9b1c3e5a8"
down_revision = "c6e8a0b2d4f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute("ALTER TYPE workspacefilecategory ADD VALUE IF NOT EXISTS 'POLICY'")


def downgrade() -> None:
    # Postgres cannot drop a single enum label. Rebuilding the type would need
    # every dependent column rewritten, and leaving an unused label costs
    # nothing, so this is deliberately a no-op.
    pass

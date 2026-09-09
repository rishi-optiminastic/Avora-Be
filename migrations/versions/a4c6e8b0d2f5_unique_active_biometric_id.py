"""Stop two ACTIVE employees sharing one biometric id.

A duplicate id made `get_by_biometric_id` raise MultipleResultsFound, and because
the connector resolves every punch in a batch, one duplicate rejected the entire
upload with a 500. A whole day of office punches was silently lost with nothing
on screen to explain it.

The resolver now tolerates duplicates, but the pair should not exist among people
currently employed. This index is partial on `is_active` on purpose: offboarding
soft-deletes the row and keeps it (rule 8), so a rejoiner re-enrolled on the same
finger legitimately has an old inactive record carrying the same id. Only the
live roster has to be unambiguous.

Revision ID: a4c6e8b0d2f5
Revises: f3b5d7a9c1e4
"""

from __future__ import annotations

from alembic import op

revision = "a4c6e8b0d2f5"
down_revision = "f3b5d7a9c1e4"
branch_labels = None
depends_on = None

_INDEX = "uq_employees_active_biometric_id"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "employees",
        ["biometric_id"],
        unique=True,
        postgresql_where="is_active AND biometric_id IS NOT NULL AND biometric_id <> ''",
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="employees")

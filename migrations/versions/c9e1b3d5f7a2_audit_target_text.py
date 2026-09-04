"""Widen audit_log.target to TEXT.

The payroll export records exactly whose bank details left the system (rule
5.7), which is a list of employee ids. Seventeen people overflow varchar(256),
and because the audit write happens inside the request the insert failure took
the whole export down with it — HR saw "export failed" with no way to tell why.

Widening is safe for the hash chain: `entry_hash` is computed from the target
STRING, and no existing value changes.

Revision ID: c9e1b3d5f7a2
Revises: b8d0f2a4c6e9
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9e1b3d5f7a2"
down_revision = "b8d0f2a4c6e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "audit_log",
        "target",
        existing_type=sa.String(length=256),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Anything already longer than 256 would not fit again; truncate first so the
    # narrowing cannot fail on real data.
    op.execute(sa.text("UPDATE audit_log SET target = left(target, 256) WHERE target IS NOT NULL"))
    op.alter_column(
        "audit_log",
        "target",
        existing_type=sa.Text(),
        type_=sa.String(length=256),
        existing_nullable=True,
    )

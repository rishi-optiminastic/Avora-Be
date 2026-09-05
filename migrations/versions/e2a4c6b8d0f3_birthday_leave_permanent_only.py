"""Birthday leave is for permanent employees only.

The written policy lists Birthday Leave as "Permanent", so it does not apply
during probation. The probation band was seeded granting 1 day.

Seeding is additive — `seed_defaults` never rewrites an existing row, precisely
so it cannot undo a deliberate org edit — which means changing the seed alone
would not move production. The existing row has to be updated here.

Only touches a row still sitting on the seeded 1: if HR has already set this
band's birthday allowance by hand, that decision outranks this migration.

Revision ID: e2a4c6b8d0f3
Revises: d1f3a5c7e9b2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2a4c6b8d0f3"
down_revision = "d1f3a5c7e9b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE leave_tier_quotas SET annual_days = 0 "
            "WHERE tier = 'PROBATION'::tenurestatus "
            "  AND leave_type = 'BIRTHDAY'::leavetype AND annual_days = 1"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE leave_tier_quotas SET annual_days = 1 "
            "WHERE tier = 'PROBATION'::tenurestatus "
            "  AND leave_type = 'BIRTHDAY'::leavetype AND annual_days = 0"
        )
    )

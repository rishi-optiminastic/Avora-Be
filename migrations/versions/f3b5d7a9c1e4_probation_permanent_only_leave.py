"""Withhold permanent-only leave during probation.

Leave Policy 2026 makes marriage, paternity and maternity leave conditional on
permanent employee status, and grants parent bereavement leave "from Day 1,
including during probation". The probation band only restricted planned, annual,
sick and birthday, so the other three fell through to the org policy and were
available to people still on probation.

Seeding is additive — `seed_defaults` never rewrites an existing row so it cannot
undo a deliberate org edit — which means the new rows have to be inserted here
for any environment whose tier table is already populated.

`tier` and `leave_type` are Postgres ENUMS (tenurestatus / leavetype). A bound
parameter arrives as varchar and Postgres will not implicitly cast it to an enum
in a comparison — "operator does not exist: leavetype = character varying" — so
every parameter is cast explicitly. A bare literal coerces fine, which is what
makes this easy to miss.

Revision ID: f3b5d7a9c1e4
Revises: e2a4c6b8d0f3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3b5d7a9c1e4"
down_revision = "e2a4c6b8d0f3"
branch_labels = None
depends_on = None

_WITHHELD = ("MARRIAGE", "PATERNITY", "MATERNITY")


def upgrade() -> None:
    for leave_type in _WITHHELD:
        # Only insert when the band has no opinion yet; an existing row is the
        # org's own decision and outranks the policy default.
        op.execute(
            sa.text(
                "INSERT INTO leave_tier_quotas "
                "  (id, tier, leave_type, annual_days, created_at, updated_at) "
                "SELECT gen_random_uuid(), 'PROBATION'::tenurestatus, "
                "       CAST(:lt AS leavetype), 0, now(), now() "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM leave_tier_quotas "
                "  WHERE tier = 'PROBATION'::tenurestatus "
                "    AND leave_type = CAST(:lt AS leavetype)"
                ")"
            ).bindparams(lt=leave_type)
        )


def downgrade() -> None:
    for leave_type in _WITHHELD:
        op.execute(
            sa.text(
                "DELETE FROM leave_tier_quotas "
                "WHERE tier = 'PROBATION'::tenurestatus "
                "  AND leave_type = CAST(:lt AS leavetype) "
                "  AND annual_days = 0"
            ).bindparams(lt=leave_type)
        )

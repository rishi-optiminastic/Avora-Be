"""INTERN replaces EMPLOYEE on the role enum

Org decision (2026-10-17): Avora stops calling people "Employee". Everyone who
carried that role becomes EXECUTIVE - the ordinary, self-scoped default - and
INTERN is reserved for people actually on an internship.

Three things worth knowing about this migration:

* The `role` column is a native Postgres ENUM whose labels are the StrEnum
  MEMBER NAMES, so they are uppercase: 'EMPLOYEE', 'EXECUTIVE', 'INTERN'.
* A new enum label cannot be used in the transaction that added it, so the
  ALTER TYPE runs in an autocommit block before any row is touched.
* 'EMPLOYEE' is deliberately left in the type. Postgres cannot drop an enum
  label without rebuilding the type, and an unused label is harmless. After
  this runs no row holds it, which is what matters - the Python enum no longer
  has a member to map it onto.

Both `employees.role` and `invitations.role` share the type, so a pending invite
issued as EMPLOYEE has to move too. Miss it and accepting that invite fails to
load with a ValueError on an unknown enum member.

Revision ID: b4d6f8a0c2e4
Revises: f2c4e6a8b0d3
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4d6f8a0c2e4"
down_revision: str | None = "f2c4e6a8b0d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The one person who is genuinely an intern. Matched on name rather than id
# because ids differ between environments; the match is anchored and scoped to
# active people so it cannot quietly demote a leaver or a namesake who left.
# If it matches nobody the migration still succeeds - nobody is an intern, and
# an admin can set it in Settings -> Roles - so this never blocks a deploy.
_INTERN_NAME_PREFIX = "anup"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE role ADD VALUE IF NOT EXISTS 'INTERN'")

    op.execute("UPDATE employees SET role = 'EXECUTIVE' WHERE role = 'EMPLOYEE'")
    op.execute("UPDATE invitations SET role = 'EXECUTIVE' WHERE role = 'EMPLOYEE'")

    # Bound, not interpolated (rule 5.6), even though the value is a constant
    # in this file: a migration that formats SQL by hand is the template the
    # next one gets copied from.
    op.execute(
        sa.text(
            "UPDATE employees SET role = 'INTERN' "
            "WHERE is_active AND full_name ILIKE :prefix"
        ).bindparams(prefix=f"{_INTERN_NAME_PREFIX}%")
    )


def downgrade() -> None:
    # Asymmetric on purpose. Which EXECUTIVEs used to be EMPLOYEEs is not
    # recorded anywhere, so restoring that split is impossible; what the old code
    # actually needs is for no row to hold a label its Role enum lacks, and
    # moving INTERN back to EMPLOYEE achieves exactly that.
    op.execute("UPDATE employees SET role = 'EMPLOYEE' WHERE role = 'INTERN'")
    op.execute("UPDATE invitations SET role = 'EMPLOYEE' WHERE role = 'INTERN'")

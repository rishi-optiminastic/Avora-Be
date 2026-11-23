"""Provident Fund ceiling raised to a ₹25,000 Basic (a ₹3,000 cap).

Revision ID: a2c4e6f8b0d3
Revises: f5a7c9e1b3d6
Create Date: 2026-11-20

PF is 12% of Basic up to a ₹25,000 monthly Basic, which caps the deduction at
₹3,000. The cap is a stored org setting, so changing the model default alone
would leave every existing workspace on the old ₹1,800 and the calculator would
keep deducting it.

Guarded on the OLD DEFAULT: a workspace that deliberately typed its own cap into
Settings keeps it. Verify the data, not the revision number - a guarded
migration whose guard misses is a silent no-op:

    SELECT pf_cap_minor FROM payroll_settings;
"""

from __future__ import annotations

from alembic import op

revision = "a2c4e6f8b0d3"
down_revision = "f5a7c9e1b3d6"
branch_labels = None
depends_on = None

# ₹1,800 is 12% of a ₹15,000 Basic; ₹3,000 is 12% of a ₹25,000 Basic. Written as
# literals in the statement rather than interpolated, per CLAUDE §5.6.


def upgrade() -> None:
    op.execute("UPDATE payroll_settings SET pf_cap_minor = 300000 WHERE pf_cap_minor = 180000")


def downgrade() -> None:
    op.execute("UPDATE payroll_settings SET pf_cap_minor = 180000 WHERE pf_cap_minor = 300000")

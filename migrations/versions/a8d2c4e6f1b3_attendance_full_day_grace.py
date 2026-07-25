"""attendance_policy.full_day_grace_minutes — hours-side grace band

Adds a grace band on the full-day HOURS threshold, mirroring the arrival
`buffer_minutes`. Before this, working one minute short of `full_day_min_minutes`
dropped a day straight to half day (a hard cliff), while arrival already got
`buffer_minutes` + `regularization_window_minutes` of tolerance. Default 15 keeps
parity with the arrival buffer; set 0 for the previous hard cutoff.

Backward-compatible: server_default backfills the existing singleton row, then is
dropped so the model default owns the value going forward.

Revision ID: a8d2c4e6f1b3
Revises: <SET TO DEPLOYED HEAD — see note below>
Create Date: 2026-07-24 00:00:00.000000

NOTE (down_revision): this repo currently has multiple un-merged Alembic heads
from parallel feature branches, so `alembic upgrade head` will error with
"multiple heads" until they are reconciled. Before applying this migration, set
`down_revision` below to the revision reported by `alembic current` on the target
database (the actually-deployed head), or add an `alembic merge` first. It is left
as None here deliberately so it is not silently chained onto the wrong lineage.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8d2c4e6f1b3"
# TODO: set to the deployed head (see module docstring) before running.
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attendance_policy",
        sa.Column(
            "full_day_grace_minutes", sa.Integer(), nullable=False, server_default="15"
        ),
    )
    op.alter_column("attendance_policy", "full_day_grace_minutes", server_default=None)


def downgrade() -> None:
    op.drop_column("attendance_policy", "full_day_grace_minutes")

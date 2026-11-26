"""meeting reminder sent

Records when the "your meeting is soon" nudge went out, so the scheduler can
send it exactly once.

The scheduler ticks every few minutes and the reminder window is measured in
tens of minutes, so without somewhere to record the send, every tick inside
that window would nudge the same people again. A nullable timestamp is enough:
NULL means not yet, and a value means done.

Backfilled to NULL on purpose. Every meeting already confirmed when this ships
will get one reminder if it has not happened yet, which is the behaviour people
would expect, and none at all if it already has - `due_for_reminder` only looks
forward.

Revision ID: f4a6b8d0c2e5
Revises: e3f5a7c9b1d4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f4a6b8d0c2e5"
down_revision = "e3f5a7c9b1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_requests",
        sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The scheduler's hot query is "confirmed, starting soon, not yet
    # reminded". Partial on the NULL so the index holds only the rows still
    # waiting for a nudge rather than every meeting the company has ever run.
    op.create_index(
        "ix_meeting_requests_reminder_due",
        "meeting_requests",
        ["scheduled_start_at"],
        unique=False,
        postgresql_where=sa.text("reminder_sent_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_meeting_requests_reminder_due", table_name="meeting_requests")
    op.drop_column("meeting_requests", "reminder_sent_at")

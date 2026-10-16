"""Device capture health: the agent's latest self-report, plus a notification kind.

The agent now POSTs /devices/health every few minutes (and on change) saying
whether it can take screenshots, read window titles and browser URLs, and how
much it has queued offline. The latest report lives on the device row; every
column is nullable because devices enrolled before this (and old agents that
never report) have none.

`health_attention_since` marks an open "needs attention" incident so admins are
alerted once per incident, not on every report.

The `notificationkind` native enum stores member NAMES, so the new member adds
the label 'DEVICE_HEALTH'. Postgres 12+ allows ADD VALUE inside a transaction as
long as the value is not used in the same transaction (it is not here).

Revision ID: d9a1c3e5f7b2
Revises: c8f0b2d4e6a9
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d9a1c3e5f7b2"
down_revision = "c8f0b2d4e6a9"
branch_labels = None
depends_on = None

_TABLE = "devices"


def _columns() -> list[sa.Column[object]]:
    return [
        sa.Column("health_agent_version", sa.String(32), nullable=True),
        sa.Column("health_os", sa.String(16), nullable=True),
        sa.Column("health_screen_capture", sa.String(24), nullable=True),
        sa.Column("health_window_info", sa.String(24), nullable=True),
        sa.Column("health_browser_url", sa.String(24), nullable=True),
        sa.Column("health_last_error", sa.String(300), nullable=True),
        sa.Column("health_queue_items", sa.Integer(), nullable=True),
        sa.Column("health_queue_bytes", sa.BigInteger(), nullable=True),
        sa.Column("health_oldest_queued_age_seconds", sa.Integer(), nullable=True),
        sa.Column("health_reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_attention_since", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.execute("ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS 'DEVICE_HEALTH'")
    for column in _columns():
        op.add_column(_TABLE, column)


def downgrade() -> None:
    for column in reversed(_columns()):
        op.drop_column(_TABLE, column.name)
    # Postgres cannot drop a single enum value without recreating the type; the
    # extra label is harmless, so it stays.

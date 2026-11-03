"""worker_heartbeats — durable liveness for the background schedulers

The schedulers already pinged an external heartbeat URL, but that is opt-in per
worker through an env var, and an env var nobody set looks exactly like a healthy
worker. Five of eight had none, including the celebrations scheduler, which was
killed and never restarted - so no birthday, anniversary or festival email went
out for seven weeks and nothing anywhere said so.

A row here means "this worker reached the database". The ABSENCE of a row is the
signal an outward ping can never give. See app/core/workers.py.

Revision ID: f7b9d1e3a5c7
Revises: e6a8c2d4f6b8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7b9d1e3a5c7"
down_revision = "e6a8c2d4f6b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        # The worker's short name is the natural key: one row per worker, and
        # upserting by name is the entire access pattern.
        sa.Column("name", sa.String(length=64), primary_key=True),
        # Nullable: a row with no beat is a worker that is declared but has
        # never run, which is the case an outward ping cannot report. The row
        # exists so there is somewhere to record that we already alerted.
        sa.Column("last_beat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")

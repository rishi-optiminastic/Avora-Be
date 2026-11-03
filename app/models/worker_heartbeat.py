"""Worker liveness, recorded in the database rather than only pinged outwards.

The schedulers already ping an external heartbeat URL, but that is opt-in per
worker through an env var - and an env var nobody set looks exactly like a
healthy worker. Five of the eight had no URL configured, including the two that
were silently dead: celebrations (killed, never restarted, so no birthday,
anniversary or festival email went out for seven weeks) and forms.

A row here says "this worker reached the database at this time". The absence of a
row is the signal the URL ping can never give: a worker that was written, maybe
even deployed once, and is not running now.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class WorkerHeartbeat(TimestampMixin, Base):
    __tablename__ = "worker_heartbeats"

    # The worker's short name (`celebrations`, `eod`, ...), which is also its key
    # in `EXPECTED_WORKERS`. Natural primary key: there is exactly one row per
    # worker and upserting by name is the whole access pattern.
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    # NULL means declared but never seen - the worker that was written, maybe
    # deployed once, and is not running. The row exists so there is somewhere to
    # record that we already raised the alarm about it.
    last_beat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # When someone was last told this worker is down. Cleared on recovery, so a
    # worker that dies again raises a fresh alarm rather than staying quiet.
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

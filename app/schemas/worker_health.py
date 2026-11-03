"""Worker-health read models (Golden rule #5)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class WorkerState(StrEnum):
    OK = "ok"
    STALE = "stale"  # it ran once and has gone quiet
    NEVER_SEEN = "never_seen"  # declared, but has never reached the database


class WorkerHealthRead(BaseModel):
    name: str
    label: str
    state: WorkerState
    last_beat_at: datetime | None
    minutes_since: int | None
    max_silence_minutes: int
    # What stops working while this one is down, so an alert can say so.
    impact: str


class WorkerHealthSummary(BaseModel):
    checked_at: datetime
    healthy: bool
    workers: list[WorkerHealthRead]

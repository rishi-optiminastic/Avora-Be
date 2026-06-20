"""Quick Meet schemas (Golden rule #5)."""

from __future__ import annotations

from pydantic import BaseModel


class QuickMeetingRead(BaseModel):
    meet_url: str
    slack_posted: bool
    started_by: str

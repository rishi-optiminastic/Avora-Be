"""Announcement request/response schemas.

`AnnouncementRead` is the unified shape rendered in the dashboard bar: it covers
both stored HR/Admin announcements (`kind=custom`, a real uuid id) and derived
"holiday tomorrow" notices (`kind=holiday`, a synthetic string id). Only custom
ones are deletable, so the client keys the delete affordance off `kind`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.models.announcement import Announcement, AnnouncementLevel


class AnnouncementKind(StrEnum):
    CUSTOM = "custom"  # posted by HR/Admin, stored, deletable
    HOLIDAY = "holiday"  # derived from the holiday calendar, ephemeral


class AnnouncementCreate(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    level: AnnouncementLevel = AnnouncementLevel.INFO
    expires_at: datetime | None = None

    @field_validator("message")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Announcement message cannot be empty.")
        return cleaned


class AnnouncementRead(BaseModel):
    id: str  # uuid string for custom; "holiday-YYYY-MM-DD" for derived notices
    message: str
    level: AnnouncementLevel
    kind: AnnouncementKind
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Announcement) -> AnnouncementRead:
        return cls(
            id=str(m.id),
            message=m.message,
            level=m.level,
            kind=AnnouncementKind.CUSTOM,
            created_at=m.created_at,
        )

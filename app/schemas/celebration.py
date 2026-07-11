"""Celebration + festival schemas (Golden rule #5 — never return ORM models)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.celebration_settings import CelebrationSettings
from app.schemas.common import ORMModel


class CelebrationSettingsRead(BaseModel):
    birthday_enabled: bool
    anniversary_enabled: bool
    festival_enabled: bool
    updated_at: datetime

    @classmethod
    def from_model(cls, m: CelebrationSettings) -> CelebrationSettingsRead:
        return cls(
            birthday_enabled=m.birthday_enabled,
            anniversary_enabled=m.anniversary_enabled,
            festival_enabled=m.festival_enabled,
            updated_at=m.updated_at,
        )


class CelebrationSettingsUpdate(BaseModel):
    """Partial update of the org celebration toggles (HR/Admin only)."""

    birthday_enabled: bool | None = None
    anniversary_enabled: bool | None = None
    festival_enabled: bool | None = None


class FestivalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    on_date: date
    message: str = Field(min_length=1, max_length=2000)
    is_active: bool = True


class FestivalRead(ORMModel):
    id: uuid.UUID
    name: str
    on_date: date
    message: str
    is_active: bool
    created_at: datetime

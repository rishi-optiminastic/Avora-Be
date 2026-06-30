"""EOD-settings schemas. The schedule is exposed as "HH:MM" strings for the UI
and stored as hour + minute ints on the model."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.eod_settings import EodSettings

_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


def hhmm(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def split_hhmm(value: str) -> tuple[int, int]:
    hours, mins = value.split(":")
    return int(hours), int(mins)


class EodSettingsRead(BaseModel):
    enabled: bool
    draft_time: str  # "HH:MM" local (attendance-policy tz)
    send_time: str  # "HH:MM" local

    @classmethod
    def from_model(cls, m: EodSettings) -> EodSettingsRead:
        return cls(
            enabled=m.enabled,
            draft_time=hhmm(m.draft_hour, m.draft_minute),
            send_time=hhmm(m.send_hour, m.send_minute),
        )


class EodSettingsUpdate(BaseModel):
    enabled: bool | None = None
    draft_time: str | None = Field(default=None, pattern=_HHMM)
    send_time: str | None = Field(default=None, pattern=_HHMM)

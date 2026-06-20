"""Attendance-policy schemas. Times are exposed as "HH:MM" for the UI and stored
as minutes-from-midnight on the model."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.attendance_policy import AttendancePolicy

_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def to_minutes(value: str) -> int:
    hours, mins = value.split(":")
    return int(hours) * 60 + int(mins)


class AttendancePolicyRead(BaseModel):
    work_start: str
    work_end: str
    buffer_minutes: int
    regularization_window_minutes: int
    full_day_min_minutes: int
    half_day_min_minutes: int
    monthly_regularizations: int
    timezone: str

    @classmethod
    def from_model(cls, m: AttendancePolicy) -> AttendancePolicyRead:
        return cls(
            work_start=hhmm(m.work_start_minute),
            work_end=hhmm(m.work_end_minute),
            buffer_minutes=m.buffer_minutes,
            regularization_window_minutes=m.regularization_window_minutes,
            full_day_min_minutes=m.full_day_min_minutes,
            half_day_min_minutes=m.half_day_min_minutes,
            monthly_regularizations=m.monthly_regularizations,
            timezone=m.timezone,
        )


class AttendancePolicyUpdate(BaseModel):
    work_start: str | None = Field(default=None, pattern=_HHMM)
    work_end: str | None = Field(default=None, pattern=_HHMM)
    buffer_minutes: int | None = Field(default=None, ge=0, le=240)
    regularization_window_minutes: int | None = Field(default=None, ge=0, le=240)
    full_day_min_minutes: int | None = Field(default=None, ge=0, le=1440)
    half_day_min_minutes: int | None = Field(default=None, ge=0, le=1440)
    monthly_regularizations: int | None = Field(default=None, ge=0, le=31)
    timezone: str | None = Field(default=None, max_length=64)

"""Out-of-office request/response schemas (Golden rule #5).

The client states where it went and why; everything that decides whether the day
counts - who may approve, and what it does to attendance - is re-derived on the
server from the caller's own record.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.out_of_office import OutOfOfficePortion, OutOfOfficeStatus

MAX_SPAN_DAYS = 31


class OutOfOfficeCreate(BaseModel):
    """A claim that these days were worked away from the office.

    Dates are plain local office dates, not instants: attendance is reckoned in
    office-local days, and an instant would only invite a timezone conversion at
    every read.
    """

    start_day: date
    end_day: date
    portion: OutOfOfficePortion = OutOfOfficePortion.FULL_DAY
    location: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _check_span(self) -> OutOfOfficeCreate:
        if self.end_day < self.start_day:
            raise ValueError("end_day is before start_day")
        span = (self.end_day - self.start_day).days + 1
        if span > MAX_SPAN_DAYS:
            raise ValueError(f"an out-of-office entry covers at most {MAX_SPAN_DAYS} days")
        # Half a day only means something on a single date. Over a range it would
        # silently halve every day in it, which nobody intends.
        if self.portion is OutOfOfficePortion.HALF_DAY and span > 1:
            raise ValueError("a half day covers one date only")
        return self


class OutOfOfficeReview(BaseModel):
    approve: bool
    review_note: str | None = Field(default=None, max_length=500)


class OutOfOfficeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    start_day: str
    end_day: str
    portion: OutOfOfficePortion
    location: str
    reason: str
    status: OutOfOfficeStatus
    reviewed_by_id: uuid.UUID | None
    decided_at: datetime | None
    review_note: str | None
    created_at: datetime
    # Server's verdict on whether THIS caller may decide THIS request, so the UI
    # never offers a button the API would refuse.
    can_decide: bool = False

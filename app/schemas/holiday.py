"""Holiday request/response schemas (Golden rule #5)."""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, Field

from app.models.holiday import HolidayType
from app.schemas.common import ORMModel

# A field named `date` shadows the bare `date` type in the class namespace, so we
# reference the type via the module (`datetime.date`) to keep annotations valid.


class HolidayCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    date: datetime.date
    holiday_type: HolidayType = HolidayType.PUBLIC


class HolidayUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    date: datetime.date | None = None
    holiday_type: HolidayType | None = None


class HolidayRead(ORMModel):
    id: uuid.UUID
    name: str
    date: datetime.date
    holiday_type: HolidayType
    created_by: uuid.UUID | None
    updated_by: uuid.UUID | None
    created_at: datetime.datetime
    updated_at: datetime.datetime

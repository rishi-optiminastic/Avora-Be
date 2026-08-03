"""Office-location + clock-in geofence schemas (Golden rule #5: never ORM out)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.office_location import OfficeLocation


class OfficeLocationCreate(BaseModel):
    """HR/Admin adds a clock-in geofence."""

    name: str = Field(min_length=1, max_length=120)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_m: int = Field(default=150, ge=20, le=20_000)


class OfficeLocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    latitude: float
    longitude: float
    radius_m: int
    is_active: bool

    @classmethod
    def from_model(cls, m: OfficeLocation) -> OfficeLocationRead:
        return cls.model_validate(m)


class ClockInRequest(BaseModel):
    """Optional GPS the browser captured at clock-in. Coordinates are a client
    claim — the server verifies against the geofences and stores them."""

    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)


class LocationExemptUpdate(BaseModel):
    """Admin-only: exempt one employee from the clock-in geofence."""

    location_check_exempt: bool

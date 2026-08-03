"""Office locations — the geofences that gate app clock-in.

Each row is a named place (HQ, a branch) with a centre coordinate and an allowed
radius. When location-restricted clock-in is enabled, an employee's dashboard
clock-in must fall within the radius of at least one active office (unless the
employee is exempt). HR/Admin manage these; the check itself is server-side.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class OfficeLocation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "office_locations"

    name: Mapped[str] = mapped_column(String(120))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    # Allowed distance from the centre, in metres.
    radius_m: Mapped[int] = mapped_column(Integer, default=150)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

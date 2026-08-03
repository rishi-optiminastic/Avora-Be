"""Office-location data access — the geofences for clock-in. HR/Admin manage them
(authorized in the service); the clock-in check reads only the active ones."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.office_location import OfficeLocation


class OfficeLocationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> Sequence[OfficeLocation]:
        rows = await self._session.execute(
            select(OfficeLocation).order_by(OfficeLocation.name.asc())
        )
        return rows.scalars().all()

    async def list_active(self) -> Sequence[OfficeLocation]:
        rows = await self._session.execute(
            select(OfficeLocation)
            .where(OfficeLocation.is_active.is_(True))
            .order_by(OfficeLocation.name.asc())
        )
        return rows.scalars().all()

    async def get(self, location_id: uuid.UUID) -> OfficeLocation | None:
        return await self._session.get(OfficeLocation, location_id)

    async def create(
        self, *, name: str, latitude: float, longitude: float, radius_m: int
    ) -> OfficeLocation:
        row = OfficeLocation(
            name=name, latitude=latitude, longitude=longitude, radius_m=radius_m
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def delete(self, row: OfficeLocation) -> None:
        await self._session.delete(row)
        await self._session.flush()

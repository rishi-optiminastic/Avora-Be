"""Office-location management — HR/Admin add/remove the clock-in geofences.

Reads are HR/Admin only (the coordinates are an org-config concern, not employee
content). The clock-in enforcement that consumes these lives in
`WorkSessionService`, not here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.employee import Role
from app.models.office_location import OfficeLocation
from app.repositories.audit import AuditRepository
from app.repositories.office_location import OfficeLocationRepository
from app.schemas.auth import CurrentUser
from app.schemas.office_location import OfficeLocationCreate


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class OfficeLocationService:
    def __init__(self, offices: OfficeLocationRepository, audit: AuditRepository) -> None:
        self._offices = offices
        self._audit = audit

    async def list(self, caller: CurrentUser) -> Sequence[OfficeLocation]:
        if not _can_manage(caller):
            raise AuthorizationError()
        return await self._offices.list_all()

    async def create(
        self, caller: CurrentUser, payload: OfficeLocationCreate
    ) -> OfficeLocation:
        if not _can_manage(caller):
            raise AuthorizationError()
        row = await self._offices.create(
            name=payload.name.strip(),
            latitude=payload.latitude,
            longitude=payload.longitude,
            radius_m=payload.radius_m,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="attendance.office_location.create",
            target=f"office:{row.id}",
        )
        return row

    async def delete(self, caller: CurrentUser, location_id: uuid.UUID) -> None:
        if not _can_manage(caller):
            raise AuthorizationError()
        row = await self._offices.get(location_id)
        if row is None:
            raise NotFoundError()
        await self._offices.delete(row)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="attendance.office_location.delete",
            target=f"office:{location_id}",
        )

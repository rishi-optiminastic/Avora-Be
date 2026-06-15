"""Holiday business rules.

Everyone may read the calendar; only HR/admin may mutate it (Security rule 5.3).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.employee import Role
from app.models.holiday import Holiday
from app.repositories.audit import AuditRepository
from app.repositories.holiday import HolidayRepository
from app.schemas.auth import CurrentUser
from app.schemas.holiday import HolidayCreate, HolidayUpdate


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class HolidayService:
    def __init__(self, holidays: HolidayRepository, audit: AuditRepository) -> None:
        self._holidays = holidays
        self._audit = audit

    async def list_all(self, *, offset: int, limit: int) -> tuple[Sequence[Holiday], int]:
        return await self._holidays.list_all(offset=offset, limit=limit)

    async def create(self, caller: CurrentUser, payload: HolidayCreate) -> Holiday:
        if not _can_manage(caller):
            raise AuthorizationError()
        holiday = await self._holidays.create(payload, actor_id=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="holiday.create",
            target=f"holiday:{holiday.id}",
        )
        return holiday

    async def update(
        self, caller: CurrentUser, holiday_id: uuid.UUID, payload: HolidayUpdate
    ) -> Holiday:
        if not _can_manage(caller):
            raise AuthorizationError()
        holiday = await self._holidays.get(holiday_id)
        if holiday is None:
            raise NotFoundError()
        fields = payload.model_dump(exclude_unset=True)
        for key, value in fields.items():
            setattr(holiday, key, value)
        holiday.updated_by = caller.employee_id
        await self._holidays.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="holiday.update",
            target=f"holiday:{holiday.id}",
        )
        return holiday

    async def delete(self, caller: CurrentUser, holiday_id: uuid.UUID) -> None:
        if not _can_manage(caller):
            raise AuthorizationError()
        holiday = await self._holidays.get(holiday_id)
        if holiday is None:
            raise NotFoundError()
        await self._holidays.delete(holiday)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="holiday.delete",
            target=f"holiday:{holiday_id}",
        )

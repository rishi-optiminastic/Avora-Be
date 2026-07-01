"""Announcement business rules.

Every authenticated employee may *read* the current announcements; only HR/Admin
may post or retire one. On top of the stored HR announcements, this service
*derives* a "holiday today / tomorrow" notice from the holiday calendar (in the
org's attendance-policy timezone), so the bar lights up automatically the day
before a holiday without anyone posting anything. No FastAPI objects here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.announcement import AnnouncementLevel
from app.models.employee import Role
from app.repositories.announcement import AnnouncementRepository
from app.repositories.audit import AuditRepository
from app.repositories.holiday import HolidayRepository
from app.schemas.announcement import AnnouncementCreate, AnnouncementKind, AnnouncementRead
from app.schemas.auth import CurrentUser
from app.services.attendance_policy_service import AttendancePolicyService


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class AnnouncementService:
    def __init__(
        self,
        announcements: AnnouncementRepository,
        holidays: HolidayRepository,
        policy: AttendancePolicyService,
        audit: AuditRepository,
    ) -> None:
        self._announcements = announcements
        self._holidays = holidays
        self._policy = policy
        self._audit = audit

    async def list_current(self, caller: CurrentUser) -> list[AnnouncementRead]:
        """Every authenticated employee sees the same bar: HR announcements first,
        then any derived holiday notice."""
        stored = [AnnouncementRead.from_model(a) for a in await self._announcements.list_active()]
        return stored + await self._holiday_announcements()

    async def create(self, caller: CurrentUser, payload: AnnouncementCreate) -> AnnouncementRead:
        if not _can_manage(caller):
            raise AuthorizationError()
        row = await self._announcements.create(
            message=payload.message,
            level=payload.level,
            expires_at=payload.expires_at,
            created_by=caller.employee_id,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="announcement.create",
            target=f"announcement:{row.id}",
        )
        return AnnouncementRead.from_model(row)

    async def deactivate(self, caller: CurrentUser, announcement_id: uuid.UUID) -> None:
        if not _can_manage(caller):
            raise AuthorizationError()
        row = await self._announcements.get(announcement_id)
        if row is None:
            raise NotFoundError()
        await self._announcements.deactivate(row)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="announcement.deactivate",
            target=f"announcement:{announcement_id}",
        )

    async def _holiday_announcements(self) -> list[AnnouncementRead]:
        """Derive a notice when a holiday falls today or tomorrow (org timezone)."""
        spec = await self._policy.spec()
        today = datetime.now(UTC).astimezone(ZoneInfo(spec.timezone)).date()
        tomorrow = today + timedelta(days=1)
        out: list[AnnouncementRead] = []
        for holiday in await self._holidays.list_between(today, tomorrow):
            if holiday.date == tomorrow:
                message = (
                    f"🎉 Holiday tomorrow — {holiday.name} "
                    f"({holiday.date:%a, %d %b}). The office is closed."
                )
            elif holiday.date == today:
                message = f"🎉 Holiday today — {holiday.name}. Enjoy the day off!"
            else:
                continue
            out.append(
                AnnouncementRead(
                    id=f"holiday-{holiday.date.isoformat()}",
                    message=message,
                    level=AnnouncementLevel.INFO,
                    kind=AnnouncementKind.HOLIDAY,
                    created_at=None,
                )
            )
        return out

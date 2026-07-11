"""Celebration business rules — birthday / work-anniversary / festival greetings.

`run_daily` is the scheduler entrypoint: once per org-timezone day (idempotent
via `CelebrationSettings.last_run_on`) it finds today's celebrants and broadcasts
a greeting email to the whole active team, for each enabled type. Settings +
festival CRUD are HR/Admin only. Emails are best-effort — one delivery failure
never blocks the rest of the run.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Sequence
from datetime import date

from app.core.exceptions import AuthorizationError, NotFoundError
from app.core.logging import get_logger
from app.models.celebration_settings import CelebrationSettings
from app.models.employee import Employee, Role
from app.models.festival import Festival
from app.repositories.audit import AuditRepository
from app.repositories.celebration_settings import CelebrationSettingsRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.festival import FestivalRepository
from app.schemas.auth import CurrentUser
from app.schemas.celebration import (
    CelebrationSettingsUpdate,
    FestivalCreate,
)
from app.services.email_service import EmailError, EmailService

logger = get_logger("app.celebration")


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


def _same_month_day(a: date, b: date) -> bool:
    """Match a recurring anniversary/birthday: same month + day (Feb-29 → Feb-28
    in a non-leap year so the greeting isn't skipped for four years)."""
    if a.month == 2 and a.day == 29 and not (b.month == 2 and b.day == 29):
        return b.month == 2 and b.day == 28
    return a.month == b.month and a.day == b.day


def _years_between(anchor: date, today: date) -> int:
    years = today.year - anchor.year
    if (today.month, today.day) < (anchor.month, anchor.day):
        years -= 1
    return years


class CelebrationService:
    def __init__(
        self,
        settings: CelebrationSettingsRepository,
        festivals: FestivalRepository,
        employees: EmployeeRepository,
        email: EmailService,
        audit: AuditRepository,
    ) -> None:
        self._settings = settings
        self._festivals = festivals
        self._employees = employees
        self._email = email
        self._audit = audit

    async def get_or_create_settings(self) -> CelebrationSettings:
        return await self._settings.get() or await self._settings.create_default()

    async def update_settings(
        self, caller: CurrentUser, payload: CelebrationSettingsUpdate
    ) -> CelebrationSettings:
        if not _can_manage(caller):
            raise AuthorizationError()
        settings = await self.get_or_create_settings()
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(settings, key, value)
        settings.updated_by = caller.employee_id
        await self._settings.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="celebration_settings.update",
            target=f"settings:{settings.id}",
        )
        return settings

    async def list_festivals(self, caller: CurrentUser) -> Sequence[Festival]:
        if not _can_manage(caller):
            raise AuthorizationError()
        return await self._festivals.list_all()

    async def add_festival(self, caller: CurrentUser, payload: FestivalCreate) -> Festival:
        if not _can_manage(caller):
            raise AuthorizationError()
        festival = await self._festivals.create(payload)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="festival.add",
            target=f"festival:{festival.id}",
        )
        return festival

    async def delete_festival(self, caller: CurrentUser, festival_id: uuid.UUID) -> None:
        if not _can_manage(caller):
            raise AuthorizationError()
        festival = await self._festivals.get(festival_id)
        if festival is None:
            raise NotFoundError()
        await self._festivals.delete(festival)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="festival.delete",
            target=f"festival:{festival_id}",
        )

    async def run_daily(self, today: date) -> int:
        """Send today's greetings once. Returns the number of emails sent. Safe to
        call repeatedly — a second call the same day is a no-op."""
        settings = await self.get_or_create_settings()
        if settings.last_run_on == today:
            return 0
        team = list(await self._employees.list_all_active())
        recipients = [e.work_email for e in team if e.work_email]
        sent = 0
        if settings.birthday_enabled:
            sent += await self._run_birthdays(team, recipients, today)
        if settings.anniversary_enabled:
            sent += await self._run_anniversaries(team, recipients, today)
        if settings.festival_enabled:
            sent += await self._run_festivals(recipients, today)
        settings.last_run_on = today
        await self._settings.flush()
        logger.info("celebration_run_done", extra={"date": str(today), "emails": sent})
        return sent

    async def _run_birthdays(
        self, team: list[Employee], recipients: list[str], today: date
    ) -> int:
        sent = 0
        for person in team:
            if person.date_of_birth and _same_month_day(person.date_of_birth, today):
                for to in recipients:
                    sent += await self._safe_send(
                        self._email.send_birthday(to=to, person_name=person.full_name)
                    )
        return sent

    async def _run_anniversaries(
        self, team: list[Employee], recipients: list[str], today: date
    ) -> int:
        sent = 0
        for person in team:
            if not person.hire_date or not _same_month_day(person.hire_date, today):
                continue
            years = _years_between(person.hire_date, today)
            if years < 1:  # a same-year joiner has no anniversary yet
                continue
            for to in recipients:
                sent += await self._safe_send(
                    self._email.send_anniversary(to=to, person_name=person.full_name, years=years)
                )
        return sent

    async def _run_festivals(self, recipients: list[str], today: date) -> int:
        sent = 0
        for festival in await self._festivals.list_active_on(today):
            for to in recipients:
                sent += await self._safe_send(
                    self._email.send_festival(
                        to=to, festival_name=festival.name, message=festival.message
                    )
                )
        return sent

    @staticmethod
    async def _safe_send(coro: Awaitable[None]) -> int:
        """Await one email send, swallowing a delivery error so the rest of the
        broadcast still goes out."""
        try:
            await coro
            return 1
        except EmailError:
            logger.warning("celebration_email_failed")
            return 0

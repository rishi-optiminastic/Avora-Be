"""End-of-Day schedule business rules.

Everyone may READ the schedule (people should see when their report drafts and
sends); only Admin/HR may change it. The first read materialises a default row
(seeded from env) so the scheduler always has a schedule to run against.

`spec()` returns a pure, immutable `EodSchedule` for the scheduler/worker — the
same read-only-spec pattern the attendance policy uses.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, ConflictError
from app.models.employee import Role
from app.models.eod_settings import EodSettings
from app.repositories.audit import AuditRepository
from app.repositories.eod_settings import EodSettingsRepository
from app.schemas.auth import CurrentUser
from app.schemas.eod_settings import EodSettingsUpdate, split_hhmm


@dataclass(frozen=True)
class EodSchedule:
    """The EOD schedule as a pure spec for the scheduler (used each tick)."""

    enabled: bool
    draft_hour: int
    draft_minute: int
    send_hour: int
    send_minute: int

    @property
    def draft_minutes(self) -> int:
        return self.draft_hour * 60 + self.draft_minute

    @property
    def send_minutes(self) -> int:
        return self.send_hour * 60 + self.send_minute


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class EodSettingsService:
    def __init__(
        self, settings_repo: EodSettingsRepository, audit: AuditRepository, settings: Settings
    ) -> None:
        self._settings_repo = settings_repo
        self._audit = audit
        self._settings = settings

    async def get_or_create(self) -> EodSettings:
        return await self._settings_repo.get() or await self._settings_repo.create_default(
            self._settings
        )

    async def spec(self) -> EodSchedule:
        s = await self.get_or_create()
        return EodSchedule(
            enabled=s.enabled,
            draft_hour=s.draft_hour,
            draft_minute=s.draft_minute,
            send_hour=s.send_hour,
            send_minute=s.send_minute,
        )

    async def update(self, caller: CurrentUser, payload: EodSettingsUpdate) -> EodSettings:
        if not _can_manage(caller):
            raise AuthorizationError()
        s = await self.get_or_create()
        fields = payload.model_dump(exclude_unset=True)
        if "draft_time" in fields:
            s.draft_hour, s.draft_minute = split_hhmm(fields.pop("draft_time"))
        if "send_time" in fields:
            s.send_hour, s.send_minute = split_hhmm(fields.pop("send_time"))
        if "enabled" in fields:
            s.enabled = fields.pop("enabled")
        # The review window only exists if send is after draft; reject the inverse
        # so a typo can't make today's drafts "overdue" and fire instantly.
        if (s.send_hour * 60 + s.send_minute) <= (s.draft_hour * 60 + s.draft_minute):
            raise ConflictError("Send time must be later than the draft time.")
        s.updated_by = caller.employee_id
        await self._settings_repo.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="eod_settings.update",
            target=f"eod_settings:{s.id}",
        )
        return s

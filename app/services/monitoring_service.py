"""Monitoring read model — derives attendance and live activity from raw samples.

All reads are scoped to the caller's visible employee set (Security rule 5.3):
we resolve that set via the employee repository, then query activity only for
those ids. Derivation is intentionally simple and re-computable from raw data.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta

from app.core.exceptions import NotFoundError
from app.models.activity import ActivitySample
from app.repositories.activity import ActivityRepository, DailyAgg
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.monitoring import ActivityNowRead, AttendanceRead, AttendanceStatus

# Tunables (Phase 1 — move to settings/config-per-org later).
LATE_AFTER_HOUR = 10  # login at/after 10:00 UTC counts as "late"
IDLE_SAMPLE_SECONDS = 300  # a sample with ≥5 min idle marks the person idle
ONLINE_WINDOW_MINUTES = 15  # last sample within 15 min ⇒ currently online


class MonitoringService:
    def __init__(self, activity: ActivityRepository, employees: EmployeeRepository) -> None:
        self._activity = activity
        self._employees = employees

    @staticmethod
    def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
        start = datetime.combine(day.date(), time.min, tzinfo=UTC)
        return start, start + timedelta(days=1)

    @staticmethod
    def _row(employee_id: uuid.UUID, agg: DailyAgg | None) -> AttendanceRead:
        if agg is None:
            return AttendanceRead(
                employee_id=employee_id,
                status=AttendanceStatus.ABSENT,
                login_at=None,
                logout_at=None,
                worked_minutes=0,
                idle_minutes=0,
                active_minutes=0,
                productivity_pct=0,
            )
        worked = max(0, int((agg.logout_at - agg.login_at).total_seconds() // 60))
        idle = min(worked, agg.idle_seconds // 60)
        active = max(0, worked - idle)
        status = (
            AttendanceStatus.LATE
            if agg.login_at.astimezone(UTC).hour >= LATE_AFTER_HOUR
            else AttendanceStatus.PRESENT
        )
        return AttendanceRead(
            employee_id=employee_id,
            status=status,
            login_at=agg.login_at,
            logout_at=agg.logout_at,
            worked_minutes=worked,
            idle_minutes=idle,
            active_minutes=active,
            productivity_pct=round((active / worked) * 100) if worked else 0,
        )

    async def attendance(self, caller: CurrentUser, day: datetime) -> list[AttendanceRead]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        start, end = self._day_bounds(day)
        aggs = await self._activity.daily_aggregates(ids, start, end)
        return [self._row(e.id, aggs.get(e.id)) for e in employees]

    async def live_now(self, caller: CurrentUser, now: datetime) -> list[ActivityNowRead]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        since = now - timedelta(minutes=ONLINE_WINDOW_MINUTES)
        latest = await self._activity.latest_since(ids, since)
        result: list[ActivityNowRead] = []
        for e in employees:
            sample = latest.get(e.id)
            if sample is None:
                result.append(
                    ActivityNowRead(
                        employee_id=e.id,
                        online=False,
                        idle=False,
                        active_window=None,
                        idle_seconds=0,
                        last_seen_at=None,
                    )
                )
            else:
                result.append(
                    ActivityNowRead(
                        employee_id=e.id,
                        online=True,
                        idle=sample.idle_seconds >= IDLE_SAMPLE_SECONDS,
                        active_window=sample.active_window,
                        idle_seconds=sample.idle_seconds,
                        last_seen_at=sample.received_at,
                    )
                )
        return result

    async def timeline(
        self, caller: CurrentUser, employee_id: uuid.UUID, day: datetime
    ) -> Sequence[ActivitySample]:
        # Only people in the caller's scope (404 over 403 — don't leak existence).
        if not await self._employees.can_read(caller, employee_id):
            raise NotFoundError()
        start, end = self._day_bounds(day)
        return await self._activity.samples_for_employee(employee_id, start, end)

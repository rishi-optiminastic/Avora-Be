"""Biometric attendance ingest — turn device punches into work sessions.

The on-prem connector pushes raw punches (already HMAC-verified at the route).
Here we re-derive everything (Golden rule #1): resolve each punch's `external_id`
to an employee, bucket punches into the office-local day, and upsert ONE
`WorkSession` per employee-day (`source="biometric"`). Clock-in is the earliest
punch; the day stays OPEN (no clock-out → live timer keeps counting) while the
latest punch is an "in", and only closes when the latest punch is an "out" — so
someone who's punched in but not out yet isn't wrongly scored half-day/early-out.
Unmatched ids are reported, never silently dropped. This makes biometric the
formal attendance record (AttendanceService prefers work sessions) while the
laptop agent keeps feeding productivity/activity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.employee import Employee
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.work_session import WorkSessionRepository
from app.schemas.biometric import BiometricIngestResult, BiometricPunchBatch
from app.services.attendance_policy_service import AttendancePolicyService


@dataclass
class _Window:
    """A day's punch window for one employee: earliest punch, latest punch, and
    the latest punch's direction (decides whether the day is still open)."""

    first: datetime
    last: datetime
    last_dir: str


class BiometricService:
    def __init__(
        self,
        employees: EmployeeRepository,
        sessions: WorkSessionRepository,
        policy: AttendancePolicyService,
        audit: AuditRepository,
    ) -> None:
        self._employees = employees
        self._sessions = sessions
        self._policy = policy
        self._audit = audit

    async def ingest(self, batch: BiometricPunchBatch) -> BiometricIngestResult:
        spec = await self._policy.spec()
        tz = ZoneInfo(spec.timezone)

        cache: dict[str, Employee | None] = {}
        buckets: dict[tuple[uuid.UUID, str], _Window] = {}
        matched = 0
        unmatched: set[str] = set()

        for punch in batch.punches:
            employee = await self._resolve(punch.external_id, cache)
            if employee is None:
                unmatched.add(punch.external_id)
                continue
            matched += 1
            local = punch.punched_at
            if local.tzinfo is None:
                local = local.replace(tzinfo=tz)  # naive timestamps are office-local
            at_utc = local.astimezone(UTC)
            day = local.astimezone(tz).date().isoformat()
            key = (employee.id, day)
            window = buckets.get(key)
            if window is None:
                buckets[key] = _Window(first=at_utc, last=at_utc, last_dir=punch.direction)
            else:
                window.first = min(window.first, at_utc)
                if at_utc >= window.last:  # newest punch wins the direction
                    window.last = at_utc
                    window.last_dir = punch.direction

        for (employee_id, day), window in buckets.items():
            day_start, day_end = self._day_bounds(day, tz)
            await self._sessions.upsert_biometric_day(
                employee_id=employee_id,
                day_start=day_start,
                day_end=day_end,
                clock_in_at=window.first,
                clock_out_at=self._clock_out(window),
                last_at=window.last,
            )

        await self._audit.append(
            actor="biometric-connector",
            action="biometric.ingest",
            target=f"punches:{len(batch.punches)};matched:{matched}",
        )
        return BiometricIngestResult(
            received=len(batch.punches),
            matched=matched,
            sessions_upserted=len(buckets),
            unmatched_external_ids=sorted(unmatched),
        )

    async def _resolve(
        self, external_id: str, cache: dict[str, Employee | None]
    ) -> Employee | None:
        """Map a device id to an active employee: biometric_id → hr_external_id →
        work_email (so a connector can send whichever it has)."""
        if external_id in cache:
            return cache[external_id]
        employee = await self._employees.get_by_biometric_id(external_id)
        if employee is None:
            employee = await self._employees.get_by_external_id(external_id)
        if employee is None and "@" in external_id:
            employee = await self._employees.get_by_work_email(external_id.lower())
        if employee is not None and not employee.is_active:
            employee = None
        cache[external_id] = employee
        return employee

    @staticmethod
    def _clock_out(window: _Window) -> datetime | None:
        # An "out" punch closes the day. If the latest punch is an "in", the person
        # is still inside → leave open so the live board's timer keeps counting and
        # they aren't flagged early-out / half-day. "auto" (no direction sent) keeps
        # the old heuristic: a lone punch stays open, multiple closes at the last.
        if window.last_dir == "out":
            return window.last
        if window.last_dir == "in":
            return None
        return window.last if window.last > window.first else None

    @staticmethod
    def _day_bounds(local_day: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
        y, m, d = (int(x) for x in local_day.split("-"))
        start = datetime(y, m, d, tzinfo=tz)
        return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

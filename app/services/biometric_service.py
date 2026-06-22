"""Biometric attendance ingest — turn device punches into work sessions.

The on-prem connector pushes raw punches (already HMAC-verified at the route).
Here we re-derive everything (Golden rule #1): resolve each punch's `external_id`
to an employee, bucket punches into the office-local day, and upsert ONE
`WorkSession` per employee-day (`source="biometric"`) with earliest-in /
latest-out. Unmatched ids are reported, never silently dropped. This makes
biometric the formal attendance record (AttendanceService prefers work sessions)
while the laptop agent keeps feeding productivity/activity.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.employee import Employee
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.work_session import WorkSessionRepository
from app.schemas.biometric import BiometricIngestResult, BiometricPunchBatch
from app.services.attendance_policy_service import AttendancePolicyService


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
        # (employee_id, local_day) -> [earliest_utc, latest_utc]
        buckets: dict[tuple[uuid.UUID, str], list[datetime]] = {}
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
                buckets[key] = [at_utc, at_utc]
            else:
                window[0] = min(window[0], at_utc)
                window[1] = max(window[1], at_utc)

        for (employee_id, day), (first_utc, last_utc) in buckets.items():
            day_start, day_end = self._day_bounds(day, tz)
            clock_out = last_utc if last_utc > first_utc else None  # single punch ⇒ open
            await self._sessions.upsert_biometric_day(
                employee_id=employee_id,
                day_start=day_start,
                day_end=day_end,
                clock_in_at=first_utc,
                clock_out_at=clock_out,
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
    def _day_bounds(local_day: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
        y, m, d = (int(x) for x in local_day.split("-"))
        start = datetime(y, m, d, tzinfo=tz)
        return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

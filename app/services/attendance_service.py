"""Attendance reads — policy-aware, scoped to the caller.

The live single-day board prefers explicit clock-in/out (work sessions) and
falls back to activity-derived times. The date-range and monthly report are the
formal record, driven by clock-in/out and the org policy + approved
regularizations (Security rule 5.3 — every read scoped via `all_in_scope`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.attendance import PolicySpec, classify_day
from app.core.exceptions import NotFoundError, ValidationError
from app.repositories.activity import ActivityRepository, DailyAgg
from app.repositories.employee import EmployeeRepository
from app.repositories.regularization import RegularizationRepository
from app.repositories.work_session import DaySpan, WorkSessionRepository
from app.schemas.attendance_report import AttendanceDayRow, AttendanceMonthSummary
from app.schemas.auth import CurrentUser
from app.schemas.monitoring import AttendanceRead, AttendanceStatus
from app.services.attendance_policy_service import AttendancePolicyService

MAX_RANGE_DAYS = 92


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _minutes(a: datetime, b: datetime) -> int:
    return max(0, int((_aware(b) - _aware(a)).total_seconds() // 60))


@dataclass
class _DayAcc:
    """Mutable accumulator while grouping a day's sessions for one employee."""

    login: datetime
    logout: datetime | None
    worked: int = 0
    is_open: bool = False


class AttendanceService:
    def __init__(
        self,
        employees: EmployeeRepository,
        activity: ActivityRepository,
        sessions: WorkSessionRepository,
        policy: AttendancePolicyService,
        regularizations: RegularizationRepository,
    ) -> None:
        self._employees = employees
        self._activity = activity
        self._sessions = sessions
        self._policy = policy
        self._regs = regularizations

    @staticmethod
    def _local_bounds(local_date: str, tz: str) -> tuple[datetime, datetime]:
        y, m, d = (int(x) for x in local_date.split("-"))
        start = datetime(y, m, d, tzinfo=ZoneInfo(tz))
        return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

    @staticmethod
    def _local_day(dt: datetime, tz: str) -> str:
        return _aware(dt).astimezone(ZoneInfo(tz)).date().isoformat()

    # ---- single live day (session-first, activity fallback) ---------------- #
    @staticmethod
    def _resolve(
        span: DaySpan | None, agg: DailyAgg | None, now: datetime
    ) -> tuple[datetime | None, datetime | None, int, str | None]:
        if span is not None:
            logout = span.logout_at
            worked = _minutes(span.login_at, logout or now)
            return span.login_at, logout, worked, span.ip_address
        if agg is not None:
            return agg.login_at, agg.logout_at, _minutes(agg.login_at, agg.logout_at), None
        return None, None, 0, None

    async def daily(self, caller: CurrentUser, day: datetime) -> list[AttendanceRead]:
        spec = await self._policy.spec()
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        local_date = _aware(day).astimezone(ZoneInfo(spec.timezone)).date().isoformat()
        start, end = self._local_bounds(local_date, spec.timezone)
        aggs = await self._activity.daily_aggregates(ids, start, end)
        spans = await self._sessions.day_spans(ids, start, end)
        approved = await self._regs.approved_days(ids, local_date, local_date)
        now = datetime.now(UTC)
        today = now.astimezone(ZoneInfo(spec.timezone)).date().isoformat()

        rows: list[AttendanceRead] = []
        for e in employees:
            login, logout, worked, ip = self._resolve(spans.get(e.id), aggs.get(e.id), now)
            regd = local_date in approved.get(e.id, set())
            v = classify_day(login_at=login, worked_minutes=worked, regularized=regd, policy=spec)
            agg = aggs.get(e.id)
            idle = min(worked, agg.idle_seconds // 60) if agg else 0
            active = max(0, worked - idle)
            rows.append(
                AttendanceRead(
                    employee_id=e.id,
                    status=v.status,
                    login_at=login,
                    logout_at=logout,
                    worked_minutes=worked,
                    idle_minutes=idle,
                    active_minutes=active,
                    productivity_pct=round((active / worked) * 100) if worked else 0,
                    late_login=v.late_login,
                    early_logout=v.early_logout and v.status is not AttendanceStatus.ABSENT,
                    missed_logout=logout is None and login is not None and local_date < today,
                    regularizable=v.regularizable,
                    regularized=v.regularized,
                    ip_address=ip,
                )
            )
        return rows

    # ---- range + monthly report (clock-in/out driven) ---------------------- #
    async def _day_rows(
        self, caller: CurrentUser, start_date: str, end_date: str, spec: PolicySpec
    ) -> tuple[list[AttendanceDayRow], list[uuid.UUID]]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        start, _ = self._local_bounds(start_date, spec.timezone)
        _, end = self._local_bounds(end_date, spec.timezone)
        raw = await self._sessions.sessions_in_range(ids, start, end)
        approved = await self._regs.approved_days(ids, start_date, end_date)
        now = datetime.now(UTC)

        # Group sessions by (employee, local day): earliest in, latest out, worked.
        grouped: dict[tuple[uuid.UUID, str], _DayAcc] = {}
        for emp_id, cin, cout in raw:
            day = self._local_day(cin, spec.timezone)
            acc = grouped.get((emp_id, day))
            if acc is None:
                acc = _DayAcc(login=cin, logout=cout)
                grouped[(emp_id, day)] = acc
            acc.login = min(acc.login, cin)
            if cout is None:
                acc.is_open = True
            elif acc.logout is None or cout > acc.logout:
                acc.logout = cout
            acc.worked += _minutes(cin, cout or now)

        rows: list[AttendanceDayRow] = []
        for (emp_id, day), acc in grouped.items():
            regd = day in approved.get(emp_id, set())
            v = classify_day(
                login_at=acc.login, worked_minutes=acc.worked, regularized=regd, policy=spec
            )
            rows.append(
                AttendanceDayRow(
                    employee_id=emp_id,
                    day=day,
                    status=v.status,
                    login_at=acc.login,
                    logout_at=None if acc.is_open else acc.logout,
                    worked_minutes=acc.worked,
                    late_login=v.late_login,
                    regularizable=v.regularizable,
                    regularized=v.regularized,
                )
            )
        rows.sort(key=lambda r: (r.day, str(r.employee_id)))
        return rows, ids

    async def range(
        self,
        caller: CurrentUser,
        start_date: str,
        end_date: str,
        employee_id: uuid.UUID | None = None,
    ) -> list[AttendanceDayRow]:
        if start_date > end_date:
            raise ValidationError("Start date must be on or before the end date.")
        span_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
        if span_days > MAX_RANGE_DAYS:
            raise ValidationError(f"Date range can be at most {MAX_RANGE_DAYS} days.")
        spec = await self._policy.spec()
        rows, ids = await self._day_rows(caller, start_date, end_date, spec)
        if employee_id is not None:
            if employee_id not in ids:
                raise NotFoundError()
            rows = [r for r in rows if r.employee_id == employee_id]
        return rows

    async def monthly_report(self, caller: CurrentUser, month: str) -> list[AttendanceMonthSummary]:
        y, m = (int(x) for x in month.split("-"))
        first = f"{y:04d}-{m:02d}-01"
        last_day = (datetime(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)).day
        last = f"{y:04d}-{m:02d}-{last_day:02d}"
        spec = await self._policy.spec()
        rows, ids = await self._day_rows(caller, first, last, spec)

        summary = {i: AttendanceMonthSummary(employee_id=i, **_ZERO) for i in ids}
        for r in rows:
            s = summary[r.employee_id]
            s.worked_minutes += r.worked_minutes
            if r.status is AttendanceStatus.FULL_DAY:
                s.full_days += 1
            elif r.status is AttendanceStatus.HALF_DAY:
                s.half_days += 1
            elif r.status is AttendanceStatus.LATE:
                s.late_days += 1
            if r.regularized:
                s.regularized_days += 1
        return list(summary.values())


_ZERO = {
    "full_days": 0,
    "half_days": 0,
    "late_days": 0,
    "absent_days": 0,
    "regularized_days": 0,
    "worked_minutes": 0,
}

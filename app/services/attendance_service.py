"""Attendance reads — policy-aware, scoped to the caller.

Attendance times (clock-in / clock-out / worked) come from the BIOMETRIC punch
when one exists; on days with no punch they fall back to a manual clock-in or the
laptop agent's activity so active staff aren't marked absent. The agent-vs-punch
gap itself is surfaced solely on the Reconciliation page. The date-range and
monthly report are the formal record — biometric-preferred per day — plus the org
policy + approved regularizations (Security rule 5.3 — scoped via `all_in_scope`).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.attendance import PolicySpec, classify_day
from app.core.exceptions import NotFoundError, ValidationError
from app.models.employee import Employee
from app.repositories.activity import ActivityRepository, DailyAgg, idle_minutes
from app.repositories.employee import EmployeeRepository
from app.repositories.regularization import RegularizationRepository
from app.repositories.work_session import DaySpan, WorkSessionRepository
from app.schemas.attendance_report import AttendanceDayRow, AttendanceMonthSummary
from app.schemas.auth import CurrentUser
from app.schemas.monitoring import AttendanceRead, AttendanceStatus
from app.schemas.work_session import BiometricTodayRead
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
    in_source: str | None = None
    out_source: str | None = None


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

    @staticmethod
    def _day_work_end_utc(local_date: str, spec: PolicySpec) -> datetime:
        """The local date's office-window end (e.g. 4 PM), in UTC."""
        y, m, d = (int(x) for x in local_date.split("-"))
        midnight = datetime(y, m, d, tzinfo=ZoneInfo(spec.timezone))
        return (midnight + timedelta(minutes=spec.work_end_minute)).astimezone(UTC)

    # ---- single live day (biometric-preferred, then manual/agent) ---------- #
    @staticmethod
    def _resolve(
        bio: DaySpan | None,
        other: DaySpan | None,
        agg: DailyAgg | None,
        *,
        now: datetime,
        is_today: bool,
        day_end: datetime,
        prefer: str = "biometric",
    ) -> tuple[datetime | None, datetime | None, int, str | None, str | None, str | None]:
        # Source preference (toggle on the Attendance page):
        #   "biometric" (default) — the office punch is the source of truth; with no
        #     punch (WFH / not enrolled / forgot) fall back to a manual clock-in,
        #     then the agent's activity, so active people aren't marked absent.
        #   "agent" — the laptop agent's activity is the source of truth; a punch /
        #     manual session is only the fallback when the agent saw nothing.
        # The agent-vs-punch gap itself is shown only on the Reconciliation page.
        if prefer == "agent" and agg is not None:
            worked = _minutes(agg.login_at, agg.logout_at)
            return agg.login_at, agg.logout_at, worked, None, "agent", "agent"
        span = (other or bio) if prefer == "agent" else (bio or other)
        if span is not None:
            logout = span.logout_at
            if logout is not None:
                out = logout
            elif is_today:
                out = now  # still in progress today
            else:
                # Past day left open (missed clock-out): cap worked at the last
                # activity sample (≈ when the PC went off), else the office-window
                # end — never let it bleed into following days (the 195h bug).
                out = (agg.logout_at if agg is not None else None) or day_end
            worked = _minutes(span.login_at, out)
            return span.login_at, logout, worked, span.ip_address, span.in_source, span.out_source
        if agg is not None:
            worked = _minutes(agg.login_at, agg.logout_at)
            return agg.login_at, agg.logout_at, worked, None, "agent", "agent"
        return None, None, 0, None, None, None

    async def daily(
        self,
        caller: CurrentUser,
        day: datetime,
        employees: Sequence[Employee] | None = None,
        *,
        source: str = "biometric",
    ) -> list[AttendanceRead]:
        spec = await self._policy.spec()
        # Callers with the scope already resolved (dashboard overview) pass it in.
        if employees is None:
            employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        local_date = _aware(day).astimezone(ZoneInfo(spec.timezone)).date().isoformat()
        start, end = self._local_bounds(local_date, spec.timezone)
        aggs = await self._activity.daily_aggregates(ids, start, end)
        # Biometric punch drives attendance times; manual/agent sessions and
        # activity are only the fallback when there's no punch that day.
        bio = await self._sessions.biometric_day_spans(ids, start, end)
        spans = await self._sessions.day_spans(ids, start, end)
        approved = await self._regs.approved_days(ids, local_date, local_date)
        now = datetime.now(UTC)
        now_local = now.astimezone(ZoneInfo(spec.timezone))
        today = now_local.date().isoformat()
        now_minute = now_local.hour * 60 + now_local.minute
        # The office window has closed for everyone (a past day, or today past the
        # end time). Used as the floor for "the day's hours are final".
        window_closed = local_date < today or (
            local_date == today and now_minute >= spec.work_end_minute
        )
        is_today = local_date == today
        day_end = self._day_work_end_utc(local_date, spec)

        rows: list[AttendanceRead] = []
        for e in employees:
            login, logout, worked, ip, in_src, out_src = self._resolve(
                bio.get(e.id),
                spans.get(e.id),
                aggs.get(e.id),
                now=now,
                is_today=is_today,
                day_end=day_end,
                prefer=source,
            )
            regd = local_date in approved.get(e.id, set())
            # Hours are "final" — and can downgrade the day to half / flag early-out
            # — once the window has closed, OR the person deliberately clocked out
            # (the app or the auto-checkout). A biometric out-punch alone doesn't
            # count (it may just be a lunch break) and an open session never does.
            v = classify_day(
                login_at=login,
                worked_minutes=worked,
                regularized=regd,
                policy=spec,
                day_complete=window_closed or out_src in ("dashboard", "auto"),
            )
            agg = aggs.get(e.id)
            idle = idle_minutes(agg, worked) if agg else 0
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
                    clock_in_source=in_src,
                    clock_out_source=out_src,
                )
            )
        return rows

    async def my_biometric_today(self, caller: CurrentUser) -> BiometricTodayRead | None:
        """The caller's own biometric punch session for today — drives the navbar
        timer. Biometric only (agent/manual are not used here); None if no punch."""
        spec = await self._policy.spec()
        local_date = datetime.now(UTC).astimezone(ZoneInfo(spec.timezone)).date().isoformat()
        start, end = self._local_bounds(local_date, spec.timezone)
        spans = await self._sessions.biometric_day_spans([caller.employee_id], start, end)
        span = spans.get(caller.employee_id)
        if span is None:
            return None
        return BiometricTodayRead(clock_in_at=span.login_at, clock_out_at=span.logout_at)

    # ---- range + monthly report (clock-in/out driven) ---------------------- #
    async def _day_rows(
        self, caller: CurrentUser, start_date: str, end_date: str, spec: PolicySpec
    ) -> tuple[list[AttendanceDayRow], list[uuid.UUID]]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        start, _ = self._local_bounds(start_date, spec.timezone)
        _, end = self._local_bounds(end_date, spec.timezone)
        bio_raw = await self._sessions.sessions_in_range(ids, start, end, source="biometric")
        all_raw = await self._sessions.sessions_in_range(ids, start, end)
        approved = await self._regs.approved_days(ids, start_date, end_date)
        now = datetime.now(UTC)
        now_local = now.astimezone(ZoneInfo(spec.timezone))
        today = now_local.date().isoformat()
        now_minute = now_local.hour * 60 + now_local.minute

        # Group sessions by (employee, local day): earliest in, latest out, worked.
        grouped: dict[tuple[uuid.UUID, str], _DayAcc] = {}
        bio_days: set[tuple[uuid.UUID, str]] = set()

        def _accumulate(
            emp_id: uuid.UUID,
            cin: datetime,
            cout: datetime | None,
            src: str | None,
            cout_src: str | None,
        ) -> None:
            day = self._local_day(cin, spec.timezone)
            acc = grouped.get((emp_id, day))
            if acc is None:
                acc = _DayAcc(login=cin, logout=None, in_source=src)
                grouped[(emp_id, day)] = acc
            if cin < acc.login:
                acc.login, acc.in_source = cin, src
            if cout is None:
                acc.is_open = True
            elif acc.logout is None or cout > acc.logout:
                acc.logout, acc.out_source = cout, cout_src
            # Cap an open session at the day's window-end on PAST days so a forgotten
            # clock-out can't accumulate days of "worked" time (the 195h bug).
            cutoff = now if day == today else self._day_work_end_utc(day, spec)
            acc.worked += _minutes(cin, cout or cutoff)

        # Biometric punches drive each day; remember which (employee, day) they cover.
        for emp_id, cin, cout, src, cout_src, _ip in bio_raw:
            bio_days.add((emp_id, self._local_day(cin, spec.timezone)))
            _accumulate(emp_id, cin, cout, src, cout_src)
        # Fall back to manual/agent sessions only on days with no punch.
        for emp_id, cin, cout, src, cout_src, _ip in all_raw:
            if (emp_id, self._local_day(cin, spec.timezone)) in bio_days:
                continue
            _accumulate(emp_id, cin, cout, src, cout_src)

        rows: list[AttendanceDayRow] = []
        for (emp_id, day), acc in grouped.items():
            regd = day in approved.get(emp_id, set())
            window_closed = day < today or (day == today and now_minute >= spec.work_end_minute)
            out_src = None if acc.is_open else acc.out_source
            v = classify_day(
                login_at=acc.login,
                worked_minutes=acc.worked,
                regularized=regd,
                policy=spec,
                day_complete=window_closed or out_src in ("dashboard", "auto"),
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
                    clock_in_source=acc.in_source,
                    clock_out_source=None if acc.is_open else acc.out_source,
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

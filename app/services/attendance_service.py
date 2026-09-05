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

from app.core.attendance import PolicySpec, classify_day, local_minute
from app.core.exceptions import NotFoundError, ValidationError
from app.core.payroll import is_working_day
from app.models.attendance_override import AttendanceOverrideStatus
from app.models.employee import Employee
from app.repositories.activity import ActivityRepository, DailyAgg, idle_minutes
from app.repositories.attendance_override import AttendanceOverrideRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.holiday import HolidayRepository
from app.repositories.leave import LeaveRepository
from app.repositories.regularization import RegularizationRepository
from app.repositories.work_session import DaySpan, WorkSessionRepository
from app.schemas.attendance_report import AttendanceDayRow, AttendanceMonthSummary
from app.schemas.auth import CurrentUser
from app.schemas.monitoring import AttendanceRead, AttendanceStatus
from app.schemas.work_session import BiometricTodayRead
from app.services.attendance_policy_service import AttendancePolicyService

MAX_RANGE_DAYS = 92

# HR/Admin attendance overrides map onto the derived status vocabulary.
_OVERRIDE_TO_STATUS: dict[AttendanceOverrideStatus, AttendanceStatus] = {
    AttendanceOverrideStatus.FULL_DAY: AttendanceStatus.FULL_DAY,
    AttendanceOverrideStatus.HALF_DAY: AttendanceStatus.HALF_DAY,
    AttendanceOverrideStatus.ABSENT: AttendanceStatus.ABSENT,
}


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
        overrides: AttendanceOverrideRepository,
        holidays: HolidayRepository,
        leaves: LeaveRepository,
    ) -> None:
        self._employees = employees
        self._activity = activity
        self._sessions = sessions
        self._policy = policy
        self._regs = regularizations
        self._overrides = overrides
        self._holidays = holidays
        self._leaves = leaves

    @staticmethod
    def _local_bounds(local_date: str, tz: str) -> tuple[datetime, datetime]:
        y, m, d = (int(x) for x in local_date.split("-"))
        start = datetime(y, m, d, tzinfo=ZoneInfo(tz))
        return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

    @staticmethod
    def _local_day(dt: datetime, tz: str) -> str:
        return _aware(dt).astimezone(ZoneInfo(tz)).date().isoformat()

    async def _approved_leave_dates(
        self, ids: Sequence[uuid.UUID], start_date: str, end_date: str, spec: PolicySpec
    ) -> dict[uuid.UUID, set[date]]:
        """Per-employee set of local dates covered by approved paid leave in the
        inclusive [start_date, end_date] window. Used to label an unattended day
        as On leave rather than Absent."""
        if not ids:
            return {}
        start, _ = self._local_bounds(start_date, spec.timezone)
        _, end = self._local_bounds(end_date, spec.timezone)
        leaves = await self._leaves.approved_paid_in_range(ids, start, end)
        tz = ZoneInfo(spec.timezone)
        out: dict[uuid.UUID, set[date]] = {}
        for emp_id, spans in leaves.items():
            days: set[date] = set()
            for span_start, span_end, _kind in spans:
                cursor = _aware(span_start).astimezone(tz).date()
                last = _aware(span_end).astimezone(tz).date()
                while cursor <= last:
                    days.add(cursor)
                    cursor += timedelta(days=1)
            out[emp_id] = days
        return out

    async def _absence_rows(
        self,
        ids: Sequence[uuid.UUID],
        start_date: str,
        end_date: str,
        spec: PolicySpec,
        today: str,
        existing: set[tuple[uuid.UUID, str]],
    ) -> list[AttendanceDayRow]:
        """Synthesize an Absent (or On leave) row for every working day with no
        session and no override, so unattended days are visible in the range and
        monthly report. Weekends and holidays are skipped (not working days), and
        days after today are never marked absent (they simply haven't happened)."""
        first = date.fromisoformat(start_date)
        last = min(date.fromisoformat(end_date), date.fromisoformat(today))
        if last < first:
            return []
        holidays = await self._holidays.dates_in_range(first, last)
        working: list[date] = []
        cursor = first
        while cursor <= last:
            if is_working_day(cursor, spec.working_days_per_week) and cursor not in holidays:
                working.append(cursor)
            cursor += timedelta(days=1)
        if not working:
            return []
        leave_dates = await self._approved_leave_dates(ids, start_date, last.isoformat(), spec)
        rows: list[AttendanceDayRow] = []
        for emp_id in ids:
            on_leave = leave_dates.get(emp_id, set())
            for day in working:
                iso = day.isoformat()
                if (emp_id, iso) in existing:
                    continue
                status = (
                    AttendanceStatus.ON_LEAVE if day in on_leave else AttendanceStatus.ABSENT
                )
                rows.append(
                    AttendanceDayRow(
                        employee_id=emp_id,
                        day=iso,
                        status=status,
                        login_at=None,
                        logout_at=None,
                        worked_minutes=0,
                        late_login=False,
                        regularizable=False,
                        regularized=False,
                        clock_in_source=None,
                        clock_out_source=None,
                    )
                )
        return rows

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
        # The fourth late arrival in a month is a half day, so today's verdict
        # depends on the days before it.
        lates_before = await self._lates_before(ids, local_date, spec)
        # Biometric punch drives attendance times; manual/agent sessions and
        # activity are only the fallback when there's no punch that day.
        bio = await self._sessions.biometric_day_spans(ids, start, end)
        spans = await self._sessions.day_spans(ids, start, end)
        approved = await self._regs.approved_days(ids, local_date, local_date)
        overrides = await self._overrides.for_range(ids, local_date, local_date)
        leave_dates = await self._approved_leave_dates(ids, local_date, local_date, spec)
        the_day = date.fromisoformat(local_date)
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
                prior_lates_this_month=lates_before.get(e.id, 0),
            )
            agg = aggs.get(e.id)
            idle = idle_minutes(agg, worked) if agg else 0
            active = max(0, worked - idle)
            ov = overrides.get((e.id, local_date))
            if ov is not None:
                status = _OVERRIDE_TO_STATUS[ov]
            elif v.status is AttendanceStatus.ABSENT and the_day in leave_dates.get(e.id, set()):
                # No attendance, but covered by approved paid leave → On leave.
                status = AttendanceStatus.ON_LEAVE
            else:
                status = v.status
            rows.append(
                AttendanceRead(
                    employee_id=e.id,
                    status=status,
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
    @staticmethod
    def _prev_day(day: str) -> str:
        return (date.fromisoformat(day) - timedelta(days=1)).isoformat()

    async def _lates_before(
        self, ids: Sequence[uuid.UUID], day: str, spec: PolicySpec
    ) -> dict[uuid.UUID, int]:
        """How many times each employee already arrived late earlier THIS month.

        The policy allows three late arrivals a month and makes the fourth a half
        day, so the verdict for a day depends on the days before it. Scoped to the
        calendar month `day` falls in — the allowance resets each month.
        """
        month_start = f"{day[:7]}-01"
        if day <= month_start:
            return {}
        start, _ = self._local_bounds(month_start, spec.timezone)
        _, end = self._local_bounds(self._prev_day(day), spec.timezone)
        rows = await self._sessions.sessions_in_range(list(ids), start, end, source="biometric")
        first_in: dict[tuple[uuid.UUID, str], datetime] = {}
        for emp_id, started_at, *_rest in rows:
            local_day = _aware(started_at).astimezone(ZoneInfo(spec.timezone)).date().isoformat()
            key = (emp_id, local_day)
            if key not in first_in or started_at < first_in[key]:
                first_in[key] = started_at
        counts: dict[uuid.UUID, int] = {}
        for (emp_id, _), login in first_in.items():
            if local_minute(login, spec.timezone) > spec.on_time_cutoff:
                counts[emp_id] = counts.get(emp_id, 0) + 1
        return counts

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
        overrides = await self._overrides.for_range(ids, start_date, end_date)
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

        # The allowance is monthly, so seed from any lates BEFORE this range and
        # then count forward in date order — a range that starts mid-month would
        # otherwise forgive the lates the employee already used up.
        running_lates = await self._lates_before(ids, start_date, spec)
        rows: list[AttendanceDayRow] = []
        for (emp_id, day), acc in sorted(grouped.items(), key=lambda kv: (kv[0][1], str(kv[0][0]))):
            regd = day in approved.get(emp_id, set())
            window_closed = day < today or (day == today and now_minute >= spec.work_end_minute)
            out_src = None if acc.is_open else acc.out_source
            v = classify_day(
                login_at=acc.login,
                worked_minutes=acc.worked,
                regularized=regd,
                policy=spec,
                day_complete=window_closed or out_src in ("dashboard", "auto"),
                prior_lates_this_month=running_lates.get(emp_id, 0),
            )
            if v.late_login:
                running_lates[emp_id] = running_lates.get(emp_id, 0) + 1
            ov = overrides.get((emp_id, day))
            rows.append(
                AttendanceDayRow(
                    employee_id=emp_id,
                    day=day,
                    status=_OVERRIDE_TO_STATUS[ov] if ov is not None else v.status,
                    login_at=acc.login,
                    logout_at=None if acc.is_open else acc.logout,
                    worked_minutes=acc.worked,
                    late_login=v.late_login,
                    regularizable=v.regularizable and ov is None,
                    regularized=v.regularized or ov is not None,
                    clock_in_source=acc.in_source,
                    clock_out_source=None if acc.is_open else acc.out_source,
                )
            )
        # Overrides for days with no session at all (e.g. an absent day forced to
        # full/half) still need a row so the day counts in views + payroll.
        grouped_keys = set(grouped.keys())
        for (emp_id, day), status in overrides.items():
            if (emp_id, day) in grouped_keys or emp_id not in ids:
                continue
            rows.append(
                AttendanceDayRow(
                    employee_id=emp_id,
                    day=day,
                    status=_OVERRIDE_TO_STATUS[status],
                    login_at=None,
                    logout_at=None,
                    worked_minutes=0,
                    late_login=False,
                    regularizable=False,
                    regularized=True,
                    clock_in_source=None,
                    clock_out_source=None,
                )
            )
        # Working days with neither a session nor an override are real absences (or
        # approved leave) — synthesize them so they show up everywhere, not silently
        # vanish. Payroll is unaffected: it counts present = full+late+half only.
        existing = {(r.employee_id, r.day) for r in rows}
        rows.extend(
            await self._absence_rows(ids, start_date, end_date, spec, today, existing)
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
            elif r.status is AttendanceStatus.ABSENT:
                s.absent_days += 1
            elif r.status is AttendanceStatus.ON_LEAVE:
                s.leave_days += 1
            if r.regularized:
                s.regularized_days += 1
        return list(summary.values())


_ZERO = {
    "full_days": 0,
    "half_days": 0,
    "late_days": 0,
    "absent_days": 0,
    "leave_days": 0,
    "regularized_days": 0,
    "worked_minutes": 0,
}

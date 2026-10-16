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
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.attendance import (
    BAD_REPORT_MONTH,
    DayVerdict,
    PolicySpec,
    classify_day,
    local_minute,
    report_month_bounds,
)
from app.core.attendance_export import (
    AttendanceDailyRow,
    AttendanceSummaryRow,
    build_attendance_xlsx,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.core.payroll import is_working_day
from app.models.attendance_override import AttendanceOverrideStatus
from app.models.employee import Employee
from app.repositories.activity import ActivityRepository, DailyAgg, idle_minutes
from app.repositories.attendance_override import AttendanceOverrideRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository, EmploymentPeriod, LeaverEvidence
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


@dataclass(frozen=True)
class _Clock:
    """The current moment in the office timezone, read once per request so every day in a
    report is judged against the same instant."""

    now: datetime
    today: str  # local YYYY-MM-DD
    now_minute: int  # local minutes past midnight

    def window_closed(self, day: str, spec: PolicySpec) -> bool:
        """Whether `day`'s office window is over, so its hours are final."""
        return day < self.today or (day == self.today and self.now_minute >= spec.work_end_minute)


@dataclass(frozen=True)
class _Employment:
    """The days someone was on the books: from their joining date to a leaver's
    last day. None at either end means unbounded (no date on file / still here)."""

    first: date | None = None
    last: date | None = None

    def covers(self, day: date) -> bool:
        return (self.first is None or day >= self.first) and (self.last is None or day <= self.last)


_ALWAYS_EMPLOYED = _Employment()

# Daily-register labels for days that are not an attendance outcome.
NOT_YET_JOINED = "Not yet joined"
LEFT = "Left"
WEEKEND = "Weekend"
UPCOMING = "Upcoming"
HOLIDAY_PREFIX = "Holiday: "


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
        audit: AuditRepository,
    ) -> None:
        self._employees = employees
        self._activity = activity
        self._sessions = sessions
        self._policy = policy
        self._regs = regularizations
        self._overrides = overrides
        self._holidays = holidays
        self._leaves = leaves
        self._audit = audit

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

    async def working_dates(self, first: date, last: date) -> list[date]:
        """The org's working dates in [first, last] inclusive: the policy's work
        week minus holidays. Empty when the range is inverted."""
        return await self._working_dates(first, last, await self._policy.spec())

    async def _working_dates(self, first: date, last: date, spec: PolicySpec) -> list[date]:
        if last < first:
            return []
        holidays = await self._holidays.dates_in_range(first, last)
        # Offsets, not `cursor += 1 day`: stepping past `last` would overflow
        # when `last` is `date.max`.
        days = (first + timedelta(days=n) for n in range((last - first).days + 1))
        return [
            day
            for day in days
            if is_working_day(day, spec.working_days_per_week) and day not in holidays
        ]

    async def _absence_rows(
        self,
        ids: Sequence[uuid.UUID],
        start_date: str,
        end_date: str,
        spec: PolicySpec,
        today: str,
        existing: set[tuple[uuid.UUID, str]],
        employment: Mapping[uuid.UUID, _Employment],
    ) -> list[AttendanceDayRow]:
        """Synthesize an Absent (or On leave) row for every working day with no
        session and no override, so unattended days are visible in the range and
        monthly report. Weekends and holidays are skipped (not working days), and
        days after today are never marked absent (they simply haven't happened).
        Neither are days before someone joined or after a leaver's last day:
        they were not employed then, so there was nothing to be absent from."""
        first = date.fromisoformat(start_date)
        last = min(date.fromisoformat(end_date), date.fromisoformat(today))
        if last < first:
            return []
        working = await self._working_dates(first, last, spec)
        if not working:
            return []
        leave_dates = await self._approved_leave_dates(ids, start_date, last.isoformat(), spec)
        rows: list[AttendanceDayRow] = []
        for emp_id in ids:
            on_leave = leave_dates.get(emp_id, set())
            window = employment.get(emp_id, _ALWAYS_EMPLOYED)
            for day in working:
                iso = day.isoformat()
                if (emp_id, iso) in existing or not window.covers(day):
                    continue
                status = AttendanceStatus.ON_LEAVE if day in on_leave else AttendanceStatus.ABSENT
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

    @staticmethod
    def _attended_or_employed(
        window: _Employment,
        day: date,
        login: datetime | None,
        override: AttendanceOverrideStatus | None,
    ) -> bool:
        """Whether `day` gets a daily row: they were employed then, or there is a
        real punch / override for it (kept, as `_day_rows` keeps them)."""
        return window.covers(day) or login is not None or override is not None

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
        employment = await self._employment(employees, spec)
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
            if not self._attended_or_employed(
                employment.get(e.id, _ALWAYS_EMPLOYED), the_day, login, ov
            ):
                # Before they joined / after a leaver's last day, with nothing
                # recorded: no row, exactly as the range and month views skip it.
                continue
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

    @staticmethod
    def _clock(spec: PolicySpec) -> _Clock:
        now = datetime.now(UTC)
        now_local = now.astimezone(ZoneInfo(spec.timezone))
        return _Clock(
            now=now,
            today=now_local.date().isoformat(),
            now_minute=now_local.hour * 60 + now_local.minute,
        )

    async def _lates_before(
        self, ids: Sequence[uuid.UUID], day: str, spec: PolicySpec
    ) -> dict[uuid.UUID, int]:
        """How many times each employee already arrived late earlier THIS month.

        The policy allows three late arrivals a month and makes the fourth a half
        day, so the verdict for a day depends on the days before it. Scoped to the
        calendar month `day` falls in — the allowance resets each month.

        Counted by the very grouping and classification the range loop uses
        (biometric punch first, manual/agent session on days without one), so a
        late on a no-punch day counts the same whether it falls before or inside
        the requested range. Counting biometric punches only here used to make
        the monthly late tally depend on where the range happened to start.
        """
        month_start = f"{day[:7]}-01"
        if day <= month_start:
            return {}
        prev = self._prev_day(day)
        clock = self._clock(spec)
        grouped = await self._grouped_days(ids, month_start, prev, spec, clock)
        approved = await self._regs.approved_days(ids, month_start, prev)
        _, lates = self._classified(grouped, approved, spec, clock, {})
        return lates

    async def _grouped_days(
        self,
        ids: Sequence[uuid.UUID],
        start_date: str,
        end_date: str,
        spec: PolicySpec,
        clock: _Clock,
    ) -> dict[tuple[uuid.UUID, str], _DayAcc]:
        """Sessions grouped by (employee, local day): earliest in, latest out,
        worked. Biometric punches drive a day; manual/agent sessions fill in
        only on days with no punch."""
        start, _ = self._local_bounds(start_date, spec.timezone)
        _, end = self._local_bounds(end_date, spec.timezone)
        bio_raw = await self._sessions.sessions_in_range(ids, start, end, source="biometric")
        all_raw = await self._sessions.sessions_in_range(ids, start, end)
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
            cutoff = clock.now if day == clock.today else self._day_work_end_utc(day, spec)
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
        return grouped

    @staticmethod
    def _classified(
        grouped: Mapping[tuple[uuid.UUID, str], _DayAcc],
        approved: Mapping[uuid.UUID, set[str]],
        spec: PolicySpec,
        clock: _Clock,
        prior_lates: Mapping[uuid.UUID, int],
    ) -> tuple[list[tuple[uuid.UUID, str, _DayAcc, DayVerdict]], dict[uuid.UUID, int]]:
        """Classify each grouped day in date order, carrying the monthly late
        count forward from `prior_lates`. Returns the verdicts and the running
        late counts after the last day - the one rule both the range report and
        `_lates_before` count lates by."""
        running = dict(prior_lates)
        verdicts: list[tuple[uuid.UUID, str, _DayAcc, DayVerdict]] = []
        for (emp_id, day), acc in sorted(grouped.items(), key=lambda kv: (kv[0][1], str(kv[0][0]))):
            out_src = None if acc.is_open else acc.out_source
            v = classify_day(
                login_at=acc.login,
                worked_minutes=acc.worked,
                regularized=day in approved.get(emp_id, set()),
                policy=spec,
                day_complete=clock.window_closed(day, spec) or out_src in ("dashboard", "auto"),
                prior_lates_this_month=running.get(emp_id, 0),
            )
            if v.late_login:
                running[emp_id] = running.get(emp_id, 0) + 1
            verdicts.append((emp_id, day, acc, v))
        return verdicts, running

    async def _day_rows(
        self,
        ids: Sequence[uuid.UUID],
        employment: Mapping[uuid.UUID, _Employment],
        start_date: str,
        end_date: str,
        spec: PolicySpec,
    ) -> list[AttendanceDayRow]:
        """Every (employee, day) row in the range for the given (already scoped)
        employees: worked days, override-only days and synthesized absences."""
        clock = self._clock(spec)
        grouped = await self._grouped_days(ids, start_date, end_date, spec, clock)
        approved = await self._regs.approved_days(ids, start_date, end_date)
        overrides = await self._overrides.for_range(ids, start_date, end_date)

        # The allowance is monthly, so seed from any lates BEFORE this range and
        # then count forward in date order - a range that starts mid-month would
        # otherwise forgive the lates the employee already used up.
        prior_lates = await self._lates_before(ids, start_date, spec)
        verdicts, _ = self._classified(grouped, approved, spec, clock, prior_lates)
        rows: list[AttendanceDayRow] = []
        for emp_id, day, acc, v in verdicts:
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
        id_set = set(ids)
        for (emp_id, day), status in overrides.items():
            if (emp_id, day) in grouped or emp_id not in id_set:
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
            await self._absence_rows(
                ids, start_date, end_date, spec, clock.today, existing, employment
            )
        )
        rows.sort(key=lambda r: (r.day, str(r.employee_id)))
        return rows

    async def _employment(
        self, employees: Sequence[Employee], spec: PolicySpec
    ) -> dict[uuid.UUID, _Employment]:
        """Each person's employed days: joining date to (for a leaver) last day."""
        leavers = [e.id for e in employees if not e.is_active]
        evidence = await self._employees.last_known_days(leavers)
        return {
            e.id: _Employment(
                first=e.hire_date,
                last=_leaver_last_day(evidence[e.id], spec.timezone) if e.id in evidence else None,
            )
            for e in employees
        }

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
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        if employee_id is not None and employee_id not in ids:
            raise NotFoundError()
        employment = await self._employment(employees, spec)
        rows = await self._day_rows(ids, employment, start_date, end_date, spec)
        if employee_id is not None:
            rows = [r for r in rows if r.employee_id == employee_id]
        return rows

    @staticmethod
    def _month_bounds(month: str) -> tuple[str, str]:
        """The first and last local date of `month` (YYYY-MM). The route already
        rejects an unreal month; this keeps any other caller from a 500."""
        try:
            first, last = report_month_bounds(month)
        except ValueError as exc:
            raise ValidationError(BAD_REPORT_MONTH) from exc
        return first.isoformat(), last.isoformat()

    @staticmethod
    def _summarize(
        rows: Sequence[AttendanceDayRow], ids: Sequence[uuid.UUID], tz: str
    ) -> dict[uuid.UUID, AttendanceMonthSummary]:
        """Roll day rows up into one summary per employee.

        The single place the month is counted: the API report and the .xlsx both
        read it, so the sheet can never disagree with the screen.
        """
        summary = {i: AttendanceMonthSummary(employee_id=i, **_ZERO) for i in ids}
        # Collected per person so the average covers only the days that actually
        # had a punch. Folding a zero in for every absent or not-yet-worked day
        # would drag everyone's typical arrival toward midnight.
        arrivals: dict[uuid.UUID, list[int]] = defaultdict(list)
        departures: dict[uuid.UUID, list[int]] = defaultdict(list)
        for r in rows:
            s = summary[r.employee_id]
            s.worked_minutes += r.worked_minutes
            if r.login_at is not None:
                arrivals[r.employee_id].append(local_minute(r.login_at, tz))
            if r.logout_at is not None:
                departures[r.employee_id].append(local_minute(r.logout_at, tz))
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
        for employee_id, s in summary.items():
            s.avg_check_in_minutes = _mean_minute(arrivals.get(employee_id))
            s.avg_check_out_minutes = _mean_minute(departures.get(employee_id))
        return summary

    async def _month_register(
        self, caller: CurrentUser, month: str, spec: PolicySpec, *, org_wide: bool = False
    ) -> tuple[Sequence[Employee], dict[uuid.UUID, _Employment], list[AttendanceDayRow]]:
        """The month's roster (scoped, leavers included), their employed days,
        and every day row - the shared basis of the API report and the .xlsx."""
        first, last = self._month_bounds(month)
        start, _ = self._local_bounds(first, spec.timezone)
        _, end = self._local_bounds(last, spec.timezone)
        period = EmploymentPeriod(
            first_day=date.fromisoformat(first),
            last_day=date.fromisoformat(last),
            start_utc=start,
            end_utc=end,
        )
        employees = await self._employees.all_in_scope_for_period(caller, period, org_wide=org_wide)
        employment = await self._employment(employees, spec)
        rows = await self._day_rows([e.id for e in employees], employment, first, last, spec)
        return employees, employment, rows

    async def monthly_report(
        self, caller: CurrentUser, month: str, *, org_wide: bool = False
    ) -> list[AttendanceMonthSummary]:
        """Per-person totals for the month, scoped to the caller. `org_wide` is
        for payroll only, which authorizes the payroll grant itself first."""
        spec = await self._policy.spec()
        employees, _, rows = await self._month_register(caller, month, spec, org_wide=org_wide)
        summaries = self._summarize(rows, [e.id for e in employees], spec.timezone)
        for e in employees:
            summaries[e.id].employee_name = e.full_name
            summaries[e.id].department = e.department
        return list(summaries.values())

    async def export_monthly_xlsx(self, caller: CurrentUser, month: str) -> tuple[bytes, str]:
        """The month's attendance as .xlsx: a per-employee summary and a full
        calendar register (every day of the month for every person), scoped to
        whoever is asking.

        Scope is the same clause every other attendance read uses, so HR gets
        the org, a manager gets their reports, and nobody gets more by exporting
        than by looking. The roster also keeps people who left during the month
        (HR settles their final pay from it). Audited: an export leaves the
        system (§5.7).
        """
        spec = await self._policy.spec()
        employees, employment, rows = await self._month_register(caller, month, spec)
        people = {e.id: e for e in employees}

        summaries = self._summarize(rows, list(people), spec.timezone)
        summary_rows = [_summary_row(people[i], s) for i, s in summaries.items()]
        summary_rows.sort(key=lambda r: r.employee_name)

        daily_rows = await self._register_rows(employees, employment, rows, month, spec)

        xlsx = build_attendance_xlsx(summary_rows, daily_rows, month_label=month)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="attendance.export",
            target=f"month:{month}:employees:{len(summary_rows)}",
        )
        return xlsx, f"attendance-{month}.xlsx"

    async def _register_rows(
        self,
        employees: Sequence[Employee],
        employment: Mapping[uuid.UUID, _Employment],
        rows: Sequence[AttendanceDayRow],
        month: str,
        spec: PolicySpec,
    ) -> list[AttendanceDailyRow]:
        """One Daily-sheet row per employee per calendar day of the month.

        A day with an attendance row shows it (times included, even on a weekend
        or holiday). Every other day still gets a row saying why it is blank -
        weekend, holiday, not yet joined, left, or not yet happened - so the
        sheet is a complete month register rather than a list of events.
        """
        first, last = (date.fromisoformat(d) for d in self._month_bounds(month))
        holidays = {h.date: h.name for h in await self._holidays.list_between(first, last)}
        today = self._clock(spec).today
        tz = ZoneInfo(spec.timezone)
        by_key = {(r.employee_id, r.day): r for r in rows}
        days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
        out: list[AttendanceDailyRow] = []
        for person in employees:
            window = employment.get(person.id, _ALWAYS_EMPLOYED)
            for day in days:
                row = by_key.get((person.id, day.isoformat()))
                status = _register_status(
                    row,
                    day,
                    window=window,
                    off_day=_off_day_label(day, holidays, spec),
                    today=today,
                )
                out.append(_daily_row(person, day, status, row, tz))
        out.sort(key=lambda r: (r.employee_name, r.day))
        return out


def _leaver_last_day(evidence: LeaverEvidence, tz: str) -> date | None:
    """A leaver's last employed day.

    An accepted resignation's last working day is authoritative. Without one
    there is no recorded leaving date, so the latest day they punched in or were
    on approved leave is the best available stand-in: days after it read as
    "Left" rather than as absences. None when nothing at all is on file.
    """
    if evidence.last_working_day is not None:
        return evidence.last_working_day
    zone = ZoneInfo(tz)
    seen = [
        _aware(at).astimezone(zone).date()
        for at in (evidence.last_session_at, evidence.last_leave_end_at)
        if at is not None
    ]
    return max(seen) if seen else None


def _off_day_label(day: date, holidays: Mapping[date, str], spec: PolicySpec) -> str | None:
    """The Holiday or Weekend label for a day the org does not work, else None."""
    if day in holidays:
        return f"{HOLIDAY_PREFIX}{holidays[day]}"
    if not is_working_day(day, spec.working_days_per_week):
        return WEEKEND
    return None


def _status_label(status: AttendanceStatus) -> str:
    return status.value.replace("_", " ").title()


def _register_status(
    row: AttendanceDayRow | None,
    day: date,
    *,
    window: _Employment,
    off_day: str | None,
    today: str,
) -> str:
    """The Status cell of the Daily register for one employee-day."""
    if row is not None:
        # Worked (or overridden) on a weekend/holiday: say both.
        return f"{off_day} ({_status_label(row.status)})" if off_day else _status_label(row.status)
    if window.first is not None and day < window.first:
        return NOT_YET_JOINED
    if window.last is not None and day > window.last:
        return LEFT
    if off_day is not None:
        return off_day
    if day.isoformat() > today:
        return UPCOMING
    return "-"


def _daily_row(
    person: Employee, day: date, status: str, row: AttendanceDayRow | None, tz: ZoneInfo
) -> AttendanceDailyRow:
    def clock(moment: datetime | None) -> str:
        # Local time, because the sheet is read by people in the office. A naive
        # timestamp is UTC (see `_aware`), never the server's own local zone.
        return _aware(moment).astimezone(tz).strftime("%H:%M") if moment else "-"

    return AttendanceDailyRow(
        employee_name=person.full_name,
        department=person.department or "-",
        day=day.isoformat(),
        weekday=day.strftime("%a"),
        status=status,
        check_in=clock(row.login_at if row else None),
        check_out=clock(row.logout_at if row else None),
        worked_hours=round(row.worked_minutes / 60, 2) if row else 0.0,
        late="Yes" if row and row.late_login else "",
        regularized="Yes" if row and row.regularized else "",
        in_source=(row.clock_in_source if row else None) or "-",
        out_source=(row.clock_out_source if row else None) or "-",
    )


def _summary_row(person: Employee, s: AttendanceMonthSummary) -> AttendanceSummaryRow:
    return AttendanceSummaryRow(
        employee_name=person.full_name,
        department=person.department or "-",
        full_days=s.full_days,
        half_days=s.half_days,
        late_days=s.late_days,
        absent_days=s.absent_days,
        leave_days=s.leave_days,
        regularized_days=s.regularized_days,
        # Present is what payroll counts: worked days, however late.
        present_days=s.full_days + s.half_days + s.late_days,
        worked_hours=round(s.worked_minutes / 60, 2),
        avg_check_in=_clock_minute(s.avg_check_in_minutes),
        avg_check_out=_clock_minute(s.avg_check_out_minutes),
    )


def _mean_minute(values: list[int] | None) -> int | None:
    """The mean clock position of a set of local minute-of-day values.

    None when there is nothing to average, so the caller can say "never checked
    in" instead of reporting midnight.
    """
    if not values:
        return None
    return round(sum(values) / len(values))


def _clock_minute(minutes: int | None) -> str:
    """A local minute-of-day as HH:MM for the sheet, or "-" when unknown."""
    if minutes is None:
        return "-"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


_ZERO = {
    "full_days": 0,
    "half_days": 0,
    "late_days": 0,
    "absent_days": 0,
    "leave_days": 0,
    "regularized_days": 0,
    "worked_minutes": 0,
}

"""Payroll business rules — org-wide salary computation + the HR digest.

Payroll is an HR/Admin concern across the whole org (CLAUDE §5.3). Every public
method authorizes HR/Admin and the heavy reads are scoped through the caller
(`all_in_scope`), which for HR/Admin is the entire active org. Each employee's
month is: derive the salary slip from their CTC (`app.core.payroll`), then
prorate the net by attendance — present + paid-leave days over the month's
working days (weekdays minus holidays). Sends are audited (rule 5.7).
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core.exceptions import AuthorizationError, ValidationError
from app.core.payroll import (
    CalcConfig,
    compute_breakdown,
    monthly_ctc_minor,
    prorate_net,
    weekdays_in_month,
)
from app.models.compensation import PayPeriod
from app.models.employee import Role
from app.models.leave import LeaveType
from app.models.payroll_run import PayrollRun, PayrollRunSource
from app.models.payroll_settings import PayrollSettings
from app.repositories.audit import AuditRepository
from app.repositories.compensation import CompensationRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.holiday import HolidayRepository
from app.repositories.leave import LeaveRepository
from app.repositories.payroll_run import PayrollRunRepository
from app.repositories.payroll_settings import PayrollSettingsRepository
from app.schemas.attendance_report import AttendanceMonthSummary
from app.schemas.auth import CurrentUser
from app.schemas.payroll import (
    PayrollEstimateRead,
    PayrollLineRead,
    PayrollRunRead,
    PayrollSettingsRead,
    PayrollSettingsUpdate,
    SalaryBreakdownRead,
    parse_recipients,
)
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.attendance_service import AttendanceService
from app.services.email_service import EmailService
from app.services.email_templates import payroll_digest_email

# A non-human caller for the scheduler. Admin scope = whole active org; the
# sentinel id is never matched on (admin's scope clause ignores employee_id).
SYSTEM_CALLER = CurrentUser(employee_id=uuid.UUID(int=0), role=Role.ADMIN, manager_id=None)
_MONTH_NAMES = (
    "January February March April May June July August September October November December".split()
)


@dataclass(frozen=True)
class _MonthCalendar:
    year: int
    month: int
    working_dates: frozenset[date]
    timezone: str

    @property
    def working_days(self) -> int:
        return len(self.working_dates)


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


def _parse_month(month: str) -> tuple[int, int]:
    try:
        year_s, month_s = month.split("-")
        year, m = int(year_s), int(month_s)
        if not (1 <= m <= 12) or year < 1970:
            raise ValueError
    except ValueError as exc:
        raise ValidationError("Month must be in YYYY-MM format.") from exc
    return year, m


def _month_label(year: int, month: int) -> str:
    return f"{_MONTH_NAMES[month - 1]} {year}"


class PayrollService:
    def __init__(
        self,
        settings_repo: PayrollSettingsRepository,
        runs: PayrollRunRepository,
        compensation: CompensationRepository,
        employees: EmployeeRepository,
        attendance: AttendanceService,
        policy: AttendancePolicyService,
        leaves: LeaveRepository,
        holidays: HolidayRepository,
        email: EmailService,
        audit: AuditRepository,
    ) -> None:
        self._settings_repo = settings_repo
        self._runs = runs
        self._compensation = compensation
        self._employees = employees
        self._attendance = attendance
        self._policy = policy
        self._leaves = leaves
        self._holidays = holidays
        self._email = email
        self._audit = audit

    # ---- settings ---------------------------------------------------------- #
    async def get_settings_model(self) -> PayrollSettings:
        return await self._settings_repo.get() or await self._settings_repo.create_default()

    async def get_settings(self, caller: CurrentUser) -> PayrollSettingsRead:
        if not _can_manage(caller):
            raise AuthorizationError()
        return PayrollSettingsRead.from_model(await self.get_settings_model())

    async def update_settings(
        self, caller: CurrentUser, payload: PayrollSettingsUpdate
    ) -> PayrollSettingsRead:
        if not _can_manage(caller):
            raise AuthorizationError()
        s = await self.get_settings_model()
        s.pay_day_of_month = payload.pay_day_of_month
        s.currency = payload.currency.upper()
        s.pay_cycle = payload.pay_cycle
        s.auto_send_enabled = payload.auto_send_enabled
        s.recipients = ",".join(payload.recipients)
        s.basic_pct = payload.basic_pct
        s.hra_pct = payload.hra_pct
        s.pf_pct = payload.pf_pct
        s.pf_cap_minor = payload.pf_cap_minor
        s.professional_tax_minor = payload.professional_tax_minor
        s.updated_by = caller.employee_id
        await self._settings_repo.flush()
        await self._audit.append(
            actor=str(caller.employee_id), action="payroll.settings.update", target="payroll"
        )
        return PayrollSettingsRead.from_model(s)

    # ---- estimate ---------------------------------------------------------- #
    def current_month(self, tz: str) -> str:
        now = datetime.now(UTC).astimezone(ZoneInfo(tz))
        return f"{now.year:04d}-{now.month:02d}"

    async def estimate(self, caller: CurrentUser, month: str | None) -> PayrollEstimateRead:
        if not _can_manage(caller):
            raise AuthorizationError()
        s = await self.get_settings_model()
        spec = await self._policy.spec()
        period = month or self.current_month(spec.timezone)
        cal = await self._month_calendar(period, spec.timezone)
        cfg = CalcConfig(
            basic_pct=s.basic_pct,
            hra_pct=s.hra_pct,
            pf_pct=s.pf_pct,
            pf_cap_minor=s.pf_cap_minor,
            professional_tax_minor=s.professional_tax_minor,
        )

        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        summaries = {
            r.employee_id: r for r in await self._attendance.monthly_report(caller, period)
        }
        paid_leaves = await self._leave_days_by_employee(ids, cal)

        lines: list[PayrollLineRead] = []
        total_net = 0
        total_ctc = 0
        for e in employees:
            comp = await self._compensation.get_for_employee(e.id)
            mctc = (
                monthly_ctc_minor(comp.amount_minor, is_annual=comp.period is PayPeriod.ANNUAL)
                if comp is not None
                else 0
            )
            breakdown = compute_breakdown(mctc, cfg)
            present = _present_days(summaries.get(e.id))
            paid = paid_leaves.get(e.id, 0.0)
            payable = min(float(cal.working_days), present + paid)
            net = prorate_net(breakdown.net_minor, payable, cal.working_days)
            total_net += net
            total_ctc += mctc
            lines.append(
                PayrollLineRead(
                    employee_id=e.id,
                    name=e.full_name,
                    department=e.department,
                    currency=s.currency,
                    monthly_ctc_minor=mctc,
                    breakdown=SalaryBreakdownRead(**breakdown.__dict__),
                    working_days=cal.working_days,
                    present_days=present,
                    paid_leave_days=paid,
                    payable_days=payable,
                    net_minor=net,
                    missing_compensation=comp is None,
                )
            )

        return PayrollEstimateRead(
            month=period,
            currency=s.currency,
            working_days=cal.working_days,
            employee_count=len(lines),
            total_ctc_minor=total_ctc,
            total_net_minor=total_net,
            lines=lines,
        )

    # ---- send -------------------------------------------------------------- #
    async def send_digest(self, caller: CurrentUser, month: str | None) -> PayrollRunRead:
        if not _can_manage(caller):
            raise AuthorizationError()
        run = await self._send(caller, month, source=PayrollRunSource.MANUAL, actor=caller)
        if run is None:
            raise ValidationError("Add at least one payroll recipient in settings first.")
        return PayrollRunRead.from_model(run)

    async def is_auto_send_due(self) -> bool:
        """True when auto-send is on and today (org tz) is the configured pay day."""
        s = await self.get_settings_model()
        if not s.auto_send_enabled:
            return False
        spec = await self._policy.spec()
        now_local = datetime.now(UTC).astimezone(ZoneInfo(spec.timezone))
        return now_local.day == s.pay_day_of_month

    async def run_for_system(self, month: str | None = None) -> PayrollRun | None:
        """Scheduler entry point: send this month's digest once (idempotent)."""
        spec = await self._policy.spec()
        period = month or self.current_month(spec.timezone)
        if await self._runs.get_for_month(period) is not None:
            return None  # already sent this month
        return await self._send(SYSTEM_CALLER, period, source=PayrollRunSource.AUTO, actor=None)

    async def list_runs(self, caller: CurrentUser) -> list[PayrollRunRead]:
        if not _can_manage(caller):
            raise AuthorizationError()
        return [PayrollRunRead.from_model(r) for r in await self._runs.list_recent()]

    async def _send(
        self,
        caller: CurrentUser,
        month: str | None,
        *,
        source: PayrollRunSource,
        actor: CurrentUser | None,
    ) -> PayrollRun | None:
        est = await self.estimate(caller, month)
        s = await self.get_settings_model()
        recipients = parse_recipients(s.recipients)
        if not recipients:
            return None
        year, m = _parse_month(est.month)
        subject, html = payroll_digest_email(
            month_label=_month_label(year, m),
            currency=est.currency,
            lines=[
                (line.name, line.net_minor, line.payable_days, line.working_days)
                for line in est.lines
            ],
            total_net_minor=est.total_net_minor,
        )
        for recipient in recipients:
            await self._email.send(to=recipient, subject=subject, html=html)
        run = await self._runs.upsert(
            period_month=est.month,
            currency=est.currency,
            total_net_minor=est.total_net_minor,
            employee_count=est.employee_count,
            recipients=",".join(recipients),
            source=source,
            triggered_by=actor.employee_id if actor is not None else None,
        )
        await self._audit.append(
            actor=str(actor.employee_id) if actor is not None else "system:payroll-scheduler",
            action="payroll.send",
            target=f"month:{est.month}",
        )
        return run

    # ---- calendar + leave helpers ----------------------------------------- #
    async def _month_calendar(self, month: str, tz: str) -> _MonthCalendar:
        year, m = _parse_month(month)
        weekdays = weekdays_in_month(year, m)
        last_day = calendar.monthrange(year, m)[1]
        holidays = await self._holidays.dates_in_range(date(year, m, 1), date(year, m, last_day))
        working = frozenset(d for d in weekdays if d not in holidays)
        return _MonthCalendar(year=year, month=m, working_dates=working, timezone=tz)

    async def _leave_days_by_employee(
        self, employee_ids: list[uuid.UUID], cal: _MonthCalendar
    ) -> dict[uuid.UUID, float]:
        tz = ZoneInfo(cal.timezone)
        start_utc = datetime(cal.year, cal.month, 1, tzinfo=tz).astimezone(UTC)
        ny, nm = (cal.year + 1, 1) if cal.month == 12 else (cal.year, cal.month + 1)
        end_utc = datetime(ny, nm, 1, tzinfo=tz).astimezone(UTC)
        raw = await self._leaves.approved_paid_in_range(employee_ids, start_utc, end_utc)
        out: dict[uuid.UUID, float] = {}
        for emp_id, leaves in raw.items():
            total = 0.0
            for d in cal.working_dates:
                weight = 0.0
                for s, e, kind in leaves:
                    if s.astimezone(tz).date() <= d <= e.astimezone(tz).date():
                        weight = max(weight, 0.5 if kind is LeaveType.HALF_DAY else 1.0)
                total += weight
            out[emp_id] = total
        return out


def _present_days(summary: AttendanceMonthSummary | None) -> float:
    """Days worked this month. Regularized days are already FULL_DAY, so they are
    not added again; half days count as 0.5."""
    if summary is None:
        return 0.0
    return summary.full_days + summary.late_days + 0.5 * summary.half_days

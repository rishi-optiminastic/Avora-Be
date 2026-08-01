"""Payroll business rules — org-wide salary computation + the HR digest.

Payroll is an HR/Admin concern across the whole org (CLAUDE §5.3). Every public
method authorizes HR/Admin and the heavy reads are scoped through the caller
(`all_in_scope`), which for HR/Admin is the entire active org. Each employee's
month is: derive the salary slip from their CTC (`app.core.payroll`), then
prorate it to the days actually paid. Proration is on **calendar days**: weekends
and holidays are auto-paid, so only working days (weekdays minus holidays) that
were neither present nor paid leave are loss-of-pay. `payable_days = total_days -
lop_days`. Sends are audited (rule 5.7).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.core.payroll import (
    CalcConfig,
    SalaryBreakdown,
    compute_breakdown,
    days_in_month,
    employer_contributions,
    monthly_ctc_minor,
    prorate_breakdown,
    weekdays_in_month,
)
from app.core.payroll_export import PayrollExportRow, build_payroll_xlsx, fmt_register_date
from app.core.payslip_pdf import PayslipPdfData, render_payslip_pdf
from app.core.pii_crypto import decrypt_pii
from app.models.compensation import Compensation, PayPeriod
from app.models.employee import Employee, Role
from app.models.leave import LeaveType
from app.models.payroll_adjustment import (
    PayrollAdjustment,
    PayrollAdjustmentKind,
    PayrollAdjustmentTarget,
)
from app.models.payroll_run import PayrollRun, PayrollRunSource
from app.models.payroll_settings import PayrollSettings
from app.models.payslip import Payslip
from app.repositories.audit import AuditRepository
from app.repositories.compensation import CompensationRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.holiday import HolidayRepository
from app.repositories.leave import LeaveRepository
from app.repositories.org_settings import OrgSettingsRepository
from app.repositories.payroll_adjustment import PayrollAdjustmentRepository
from app.repositories.payroll_run import PayrollRunRepository
from app.repositories.payroll_settings import PayrollSettingsRepository
from app.repositories.payslip import PayslipRepository, PayslipSnapshot
from app.repositories.reimbursement import ReimbursementRepository
from app.schemas.attendance_report import AttendanceMonthSummary
from app.schemas.auth import CurrentUser
from app.schemas.payroll import (
    PayrollEstimateRead,
    PayrollFinalizeResult,
    PayrollLineRead,
    PayrollRunRead,
    PayrollSettingsRead,
    PayrollSettingsUpdate,
    PayslipRead,
    PayslipSummaryRead,
    ReleasedPayslipRead,
    SalaryBreakdownRead,
    parse_recipients,
)
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.attendance_service import AttendanceService
from app.services.email_service import EmailError, EmailService
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
    # Working dates that have already elapsed (on or before "today" in `timezone`).
    # For a finished/past month this equals `working_dates`; for the in-progress
    # month it excludes days that have not happened yet, so loss-of-pay is never
    # charged for a working day the employee has not reached.
    elapsed_working_dates: frozenset[date]
    total_days: int  # calendar days in the month (the proration denominator)
    timezone: str

    @property
    def working_days(self) -> int:
        return len(self.working_dates)

    @property
    def elapsed_working_days(self) -> int:
        return len(self.elapsed_working_dates)


def _can_manage(caller: CurrentUser) -> bool:
    return caller.can_manage_payroll


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


def _lop_override(
    adjustments: Sequence[PayrollAdjustment], computed_lop: float, total_days: int
) -> float:
    """Resolve the effective loss-of-pay days from any OVERRIDE. Both LOP_DAYS and
    PAYABLE_DAYS overrides (stored as days x 100) are complementary — payable +
    lop = total_days — so a paid-days override is turned into the matching LOP.
    LOP_DAYS wins if both are present; otherwise the computed value stands."""

    def _days(a: PayrollAdjustment) -> float:
        return min(float(total_days), max(0.0, a.amount_minor / 100))

    for a in adjustments:
        if (
            a.kind is PayrollAdjustmentKind.OVERRIDE
            and a.target is PayrollAdjustmentTarget.LOP_DAYS
        ):
            return _days(a)
    for a in adjustments:
        if (
            a.kind is PayrollAdjustmentKind.OVERRIDE
            and a.target is PayrollAdjustmentTarget.PAYABLE_DAYS
        ):
            return float(total_days) - _days(a)  # lop = total - payable
    return computed_lop


def _apply_adjustments(
    prorated: SalaryBreakdown, adjustments: Sequence[PayrollAdjustment]
) -> tuple[SalaryBreakdown, int, int]:
    """Apply field overrides + manual earnings/deductions to a prorated slip (LOP
    overrides are handled before proration). Returns (adjusted_breakdown,
    earnings_total, deductions_total); the breakdown's net already folds in the
    earnings/deductions and any NET_PAY override."""
    basic = prorated.basic_minor
    hra = prorated.hra_minor
    special = prorated.special_allowance_minor
    employee_pf = prorated.employee_pf_minor
    professional_tax = prorated.professional_tax_minor
    income_tax = prorated.income_tax_minor
    net_override: int | None = None
    for a in adjustments:
        if a.kind is not PayrollAdjustmentKind.OVERRIDE:
            continue
        if a.target is PayrollAdjustmentTarget.BASIC:
            basic = a.amount_minor
        elif a.target is PayrollAdjustmentTarget.HRA:
            hra = a.amount_minor
        elif a.target is PayrollAdjustmentTarget.SPECIAL_ALLOWANCE:
            special = a.amount_minor
        elif a.target is PayrollAdjustmentTarget.EMPLOYEE_PF:
            employee_pf = a.amount_minor
        elif a.target is PayrollAdjustmentTarget.PROFESSIONAL_TAX:
            professional_tax = a.amount_minor
        elif a.target is PayrollAdjustmentTarget.INCOME_TAX:
            income_tax = a.amount_minor
        elif a.target is PayrollAdjustmentTarget.NET_PAY:
            net_override = a.amount_minor
    gross = basic + hra + special
    total_deduction = employee_pf + professional_tax + income_tax
    earnings = sum(a.amount_minor for a in adjustments if a.kind is PayrollAdjustmentKind.EARNING)
    deductions = sum(
        a.amount_minor for a in adjustments if a.kind is PayrollAdjustmentKind.DEDUCTION
    )
    base_net = net_override if net_override is not None else gross - total_deduction
    adjusted = replace(
        prorated,
        basic_minor=basic,
        hra_minor=hra,
        special_allowance_minor=special,
        employee_pf_minor=employee_pf,
        professional_tax_minor=professional_tax,
        income_tax_minor=income_tax,
        gross_minor=gross,
        total_deduction_minor=total_deduction,
        net_minor=base_net + earnings - deductions,
    )
    return adjusted, earnings, deductions


class PayrollService:
    def __init__(
        self,
        settings_repo: PayrollSettingsRepository,
        runs: PayrollRunRepository,
        payslips: PayslipRepository,
        compensation: CompensationRepository,
        employees: EmployeeRepository,
        attendance: AttendanceService,
        policy: AttendancePolicyService,
        leaves: LeaveRepository,
        holidays: HolidayRepository,
        orgs: OrgSettingsRepository,
        reimbursements: ReimbursementRepository,
        adjustments: PayrollAdjustmentRepository,
        email: EmailService,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._settings_repo = settings_repo
        self._runs = runs
        self._payslips = payslips
        self._compensation = compensation
        self._employees = employees
        self._attendance = attendance
        self._policy = policy
        self._leaves = leaves
        self._holidays = holidays
        self._orgs = orgs
        self._reimbursements = reimbursements
        self._adjustments = adjustments
        self._email = email
        self._audit = audit
        self._settings = settings

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
        s.auto_release_enabled = payload.auto_release_enabled
        s.auto_release_day = payload.auto_release_day
        s.recipients = ",".join(payload.recipients)
        s.basic_pct = payload.basic_pct
        s.hra_pct = payload.hra_pct
        s.pf_pct = payload.pf_pct
        s.pf_cap_minor = payload.pf_cap_minor
        s.professional_tax_minor = payload.professional_tax_minor
        s.professional_tax_feb_minor = payload.professional_tax_feb_minor
        s.deduct_income_tax = payload.deduct_income_tax
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

    def previous_month(self, tz: str) -> str:
        now = datetime.now(UTC).astimezone(ZoneInfo(tz))
        year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        return f"{year:04d}-{month:02d}"

    async def estimate(self, caller: CurrentUser, month: str | None) -> PayrollEstimateRead:
        if not _can_manage(caller):
            raise AuthorizationError()
        s = await self.get_settings_model()
        spec = await self._policy.spec()
        period = month or self.current_month(spec.timezone)
        cal = await self._month_calendar(period, spec.timezone, spec.working_days_per_week)
        cfg = self._calc_config(s)

        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        summaries = {
            r.employee_id: r for r in await self._attendance.monthly_report(caller, period)
        }
        paid_leaves = await self._leave_days_by_employee(ids, cal)
        comps = await self._compensation.get_for_employees(ids)  # one query, not N
        adjustments = await self._adjustments.for_month(ids, period)

        lines: list[PayrollLineRead] = []
        total_net = 0
        total_ctc = 0
        for e in employees:
            line = await self._line_for(
                e,
                comp=comps.get(e.id),
                cfg=cfg,
                currency=s.currency,
                cal=cal,
                present=_present_days(summaries.get(e.id)),
                paid=paid_leaves.get(e.id, 0.0),
                adjustments=adjustments.get(e.id, []),
            )
            total_net += line.net_minor
            total_ctc += line.monthly_ctc_minor
            lines.append(line)

        return PayrollEstimateRead(
            month=period,
            currency=s.currency,
            total_days=cal.total_days,
            working_days=cal.working_days,
            employee_count=len(lines),
            total_ctc_minor=total_ctc,
            total_net_minor=total_net,
            lines=lines,
        )

    @staticmethod
    def _calc_config(s: PayrollSettings) -> CalcConfig:
        return CalcConfig(
            basic_pct=s.basic_pct,
            hra_pct=s.hra_pct,
            pf_pct=s.pf_pct,
            pf_cap_minor=s.pf_cap_minor,
            professional_tax_minor=s.professional_tax_minor,
            professional_tax_feb_minor=s.professional_tax_feb_minor,
            deduct_income_tax=s.deduct_income_tax,
        )

    def _export_row(
        self,
        *,
        line: PayrollLineRead,
        emp: Employee | None,
        comp: Compensation | None,
        cfg: CalcConfig,
        month: int,
        period_label: str,
        reimbursement_minor: int,
        adjustments: Sequence[PayrollAdjustment] = (),
    ) -> PayrollExportRow:
        """Build one 40-column register row. Earnings/PF are prorated on a fixed
        30-day base (Base Days minus Loss Of Pay), independent of the on-screen
        calendar-day proration; employer contributions follow standard PF rules."""
        mctc = (
            monthly_ctc_minor(comp.amount_minor, is_annual=comp.period is PayPeriod.ANNUAL)
            if comp is not None
            else 0
        )
        full = compute_breakdown(mctc, cfg, month=month)
        base_days = 30.0
        lop = min(base_days, max(0.0, line.lop_days))
        effective = base_days - lop
        p = prorate_breakdown(full, effective, int(base_days))  # 30-day-base proration
        # Manual adjustments (LOP overrides already applied to `line.lop_days` above):
        # field overrides restructure the slip; earnings/deductions fold into totals.
        p, adj_earnings, adj_deductions = _apply_adjustments(p, adjustments)
        has_net_override = any(
            a.kind is PayrollAdjustmentKind.OVERRIDE
            and a.target is PayrollAdjustmentTarget.NET_PAY
            for a in adjustments
        )
        er = employer_contributions(p.basic_minor, p.employee_pf_minor)

        def rupees(minor: int) -> int:
            return round(minor / 100)

        fixed_earnings = rupees(p.gross_minor)  # Basic + HRA + Fixed Allowance
        total_earnings = fixed_earnings + rupees(adj_earnings)  # + manual earnings
        total_deductions = rupees(p.total_deduction_minor) + rupees(adj_deductions)
        account_number = (
            decrypt_pii(self._settings, comp.account_number_encrypted)
            if comp is not None and comp.account_number_encrypted
            else ""
        )
        return PayrollExportRow(
            period=period_label,
            payroll_type="Regular Payroll",
            employee_no=(emp.employee_number or "") if emp is not None else "",
            employee_name=line.name,
            department=line.department or "",
            designation=(emp.job_title or "") if emp is not None else "",
            work_location=(emp.location or "") if emp is not None else "",
            date_of_joining=fmt_register_date(emp.hire_date if emp is not None else None),
            date_of_birth=fmt_register_date(emp.date_of_birth if emp is not None else None),
            last_working_day="",
            payment_mode=(comp.payment_mode if comp is not None else "Bank Transfer"),
            account_holder=(comp.account_holder_name or "") if comp is not None else "",
            bank_name=(comp.bank_name or "") if comp is not None else "",
            account_number=account_number,
            ifsc=(comp.ifsc_code or "") if comp is not None else "",
            ctc_annual=rupees(mctc * 12),
            gross_annual=rupees(full.gross_minor * 12),
            base_days=base_days,
            loss_of_pay=lop,
            effective_paid_days=effective,
            basic=rupees(p.basic_minor),
            hra=rupees(p.hra_minor),
            fixed_allowance=rupees(p.special_allowance_minor),
            reimbursement=0,
            total_reimbursements=0,
            fixed_monthly_earnings=fixed_earnings,
            fixed_monthly_costs=rupees(p.ctc_minor),
            total_earnings=total_earnings,
            epf_employee=rupees(p.employee_pf_minor),
            epf_employer=rupees(er.epf_employer_minor),
            eps_employer=rupees(er.eps_minor),
            edli_employer=rupees(er.edli_minor),
            epf_admin_employer=rupees(er.admin_minor),
            total_employer_contributions=rupees(er.total_minor),
            income_tax=rupees(p.income_tax_minor),
            professional_tax=rupees(p.professional_tax_minor),
            total_deductions=total_deductions,
            gross_pay=total_earnings,
            net_pay=rupees(p.net_minor) if has_net_override else total_earnings - total_deductions,
            business_expense_reimbursements=rupees(reimbursement_minor),
        )

    async def export_xlsx(self, caller: CurrentUser, month: str | None) -> tuple[bytes, str]:
        """HR/Admin: the month's payroll as an .xlsx in the 40-column "Payrun
        Employee Salary statement" register — identity + bank + per-annum CTC/Gross,
        the 30-day-base earnings/PF split, statutory employer contributions, and
        approved business-expense reimbursements, one row per employee. The account
        number is decrypted only here, for the authorized export. Audited (5.7)."""
        est = await self.estimate(caller, month)  # authorizes HR/Admin
        year, m = _parse_month(est.month)
        s = await self.get_settings_model()
        cfg = self._calc_config(s)
        ids = [line.employee_id for line in est.lines]
        comps = await self._compensation.get_for_employees(ids)
        employees = {e.id: e for e in await self._employees.all_in_scope(caller)}
        reimbursed = await self._reimbursements.approved_for_month(ids, est.month)
        adjustments = await self._adjustments.for_month(ids, est.month)
        period_label = _month_label(year, m)

        rows = [
            self._export_row(
                line=line,
                emp=employees.get(line.employee_id),
                comp=comps.get(line.employee_id),
                cfg=cfg,
                month=m,
                period_label=period_label,
                reimbursement_minor=reimbursed.get(line.employee_id, 0),
                adjustments=adjustments.get(line.employee_id, []),
            )
            for line in est.lines
        ]

        xlsx = build_payroll_xlsx(rows, month_label=_month_label(year, m), currency=est.currency)
        await self._audit.append(
            actor=str(caller.employee_id), action="payroll.export", target=f"month:{est.month}"
        )
        return xlsx, f"payroll-{est.month}.xlsx"

    async def _line_for(
        self,
        employee: Employee,
        *,
        comp: Compensation | None,
        cfg: CalcConfig,
        currency: str,
        cal: _MonthCalendar,
        present: float,
        paid: float,
        adjustments: Sequence[PayrollAdjustment] = (),
    ) -> PayrollLineRead:
        """One employee's slip for the month: CTC → breakdown → attendance proration.

        The single source of truth for a payroll line, shared by the org-wide
        `estimate()` and the self-service `my_slip()` so the two never drift. The
        caller passes the (pre-fetched) compensation so `estimate()` can batch it.
        """
        mctc = (
            monthly_ctc_minor(comp.amount_minor, is_annual=comp.period is PayPeriod.ANNUAL)
            if comp is not None
            else 0
        )
        breakdown = compute_breakdown(mctc, cfg, month=cal.month)
        # Weekends and holidays are auto-paid: only working days that were neither
        # present nor paid leave are loss-of-pay, so payable = calendar days - LOP.
        # LOP is charged only over ELAPSED working days — a working day that has not
        # happened yet (later this in-progress month) is never counted as absent, so
        # a mid-month estimate reflects days actually missed, not the calendar ahead.
        elapsed_working = float(cal.elapsed_working_days)
        worked = min(elapsed_working, present + paid)
        lop = _lop_override(adjustments, max(0.0, elapsed_working - worked), cal.total_days)
        payable = max(0.0, float(cal.total_days) - lop)
        prorated = prorate_breakdown(breakdown, payable, cal.total_days)
        prorated, adj_earnings, adj_deductions = _apply_adjustments(prorated, adjustments)
        return PayrollLineRead(
            employee_id=employee.id,
            name=employee.full_name,
            department=employee.department,
            job_title=employee.job_title,
            location=employee.location,
            hire_date=employee.hire_date,
            currency=currency,
            monthly_ctc_minor=mctc,
            breakdown=SalaryBreakdownRead(**breakdown.__dict__),
            prorated=SalaryBreakdownRead(**prorated.__dict__),
            total_days=cal.total_days,
            working_days=cal.working_days,
            elapsed_working_days=cal.elapsed_working_days,
            present_days=present,
            paid_leave_days=paid,
            lop_days=lop,
            payable_days=payable,
            adjustment_earnings_minor=adj_earnings,
            adjustment_deductions_minor=adj_deductions,
            net_minor=prorated.net_minor,
            missing_compensation=comp is None,
        )

    async def my_slip(
        self, caller: CurrentUser, employee_id: uuid.UUID | None, month: str | None
    ) -> PayslipRead:
        """One person's own slip. Self-or-HR scoped (mirrors compensation): an
        employee may read their own; HR/Admin may read anyone's. A manager
        reading a report's slip is NOT permitted here (pay is need-to-know)."""
        target_id = employee_id or caller.employee_id
        if not _can_manage(caller) and caller.employee_id != target_id:
            raise AuthorizationError()
        employee = await self._employees.get(target_id)
        if employee is None or not employee.is_active:
            raise NotFoundError()

        s = await self.get_settings_model()
        spec = await self._policy.spec()
        period = month or self.current_month(spec.timezone)
        # Release gate: a non-manager may see their OWN slip only once the month is
        # released (a frozen snapshot exists). HR/finance keep the live preview so
        # they can check numbers before releasing. This also gates the live PDF,
        # which is generated through this method.
        if not _can_manage(caller) and await self._payslips.get(target_id, period) is None:
            raise NotFoundError()
        cal = await self._month_calendar(period, spec.timezone, spec.working_days_per_week)
        cfg = CalcConfig(
            basic_pct=s.basic_pct,
            hra_pct=s.hra_pct,
            pf_pct=s.pf_pct,
            pf_cap_minor=s.pf_cap_minor,
            professional_tax_minor=s.professional_tax_minor,
            professional_tax_feb_minor=s.professional_tax_feb_minor,
            deduct_income_tax=s.deduct_income_tax,
        )
        # Self-scoped reads: monthly_report/leave lookups for just this employee.
        summaries = {
            r.employee_id: r for r in await self._attendance.monthly_report(caller, period)
        }
        paid_leaves = await self._leave_days_by_employee([target_id], cal)
        adjustments = await self._adjustments.for_month([target_id], period)
        line = await self._line_for(
            employee,
            comp=await self._compensation.get_for_employee(target_id),
            cfg=cfg,
            currency=s.currency,
            cal=cal,
            present=_present_days(summaries.get(target_id)),
            paid=paid_leaves.get(target_id, 0.0),
            adjustments=adjustments.get(target_id, []),
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="payroll.slip.read",
            target=f"employee:{target_id}:{period}",
        )
        return PayslipRead(
            month=period,
            currency=line.currency,
            monthly_ctc_minor=line.monthly_ctc_minor,
            breakdown=line.breakdown,
            prorated=line.prorated,
            total_days=line.total_days,
            working_days=line.working_days,
            elapsed_working_days=line.elapsed_working_days,
            present_days=line.present_days,
            paid_leave_days=line.paid_leave_days,
            lop_days=line.lop_days,
            payable_days=line.payable_days,
            adjustment_earnings_minor=line.adjustment_earnings_minor,
            adjustment_deductions_minor=line.adjustment_deductions_minor,
            net_minor=line.net_minor,
            missing_compensation=line.missing_compensation,
        )

    # ---- finalize + released payslips (self-service) ----------------------- #
    def _authorize_self_or_manage(
        self, caller: CurrentUser, employee_id: uuid.UUID | None
    ) -> uuid.UUID:
        """Resolve the target employee and enforce self-or-HR access (mirrors
        compensation): an employee may read their own payslips; HR/Admin may read
        anyone's. Anything else is a 403 — pay is need-to-know, no manager carve-out."""
        target = employee_id or caller.employee_id
        if not _can_manage(caller) and caller.employee_id != target:
            raise AuthorizationError()
        return target

    async def finalize(self, caller: CurrentUser, month: str | None) -> PayrollFinalizeResult:
        """HR/Admin releases a month: freeze each employee's slip into a `Payslip`
        snapshot and email everyone their PDF. Once released, employees can see and
        re-download that month. Re-running refreshes the snapshots (e.g. after an
        attendance fix). Employees with no compensation on file are skipped."""
        if not _can_manage(caller):
            raise AuthorizationError()
        est = await self.estimate(caller, month)
        released, emailed, skipped = await self._release_from_estimate(est, actor=caller)
        total_net = sum(line.net_minor for line in est.lines if not line.missing_compensation)
        await self._runs.upsert(
            period_month=est.month,
            currency=est.currency,
            total_net_minor=total_net,
            employee_count=released,
            recipients="",
            source=PayrollRunSource.MANUAL,
            triggered_by=caller.employee_id,
        )
        await self._audit.append(
            actor=str(caller.employee_id), action="payroll.finalize", target=f"month:{est.month}"
        )
        return PayrollFinalizeResult(
            month=est.month,
            currency=est.currency,
            released_count=released,
            emailed_count=emailed,
            skipped_count=skipped,
            total_net_minor=total_net,
        )

    async def list_payslips(
        self, caller: CurrentUser, employee_id: uuid.UUID | None
    ) -> list[PayslipSummaryRead]:
        """An employee's released-payslip history (self-or-HR). Default = self."""
        target = self._authorize_self_or_manage(caller, employee_id)
        records = await self._payslips.list_for_employee(target)
        return [PayslipSummaryRead.from_model(r) for r in records]

    async def get_released_payslip(
        self, caller: CurrentUser, employee_id: uuid.UUID | None, month: str | None
    ) -> ReleasedPayslipRead:
        """One released payslip from its frozen snapshot (self-or-HR). 404 until HR
        has finalized that month."""
        target = self._authorize_self_or_manage(caller, employee_id)
        spec = await self._policy.spec()
        period = month or self.current_month(spec.timezone)
        record = await self._payslips.get(target, period)
        if record is None:
            raise NotFoundError()
        return ReleasedPayslipRead.from_model(record)

    async def payslip_pdf(
        self, caller: CurrentUser, employee_id: uuid.UUID | None, month: str | None
    ) -> tuple[bytes, str]:
        """Render a released payslip to PDF bytes (self-or-HR). The download is
        audited (rule 5.7). 404 until HR has finalized that month."""
        target = self._authorize_self_or_manage(caller, employee_id)
        spec = await self._policy.spec()
        period = month or self.current_month(spec.timezone)
        record = await self._payslips.get(target, period)
        if record is None:
            raise NotFoundError()
        pdf = render_payslip_pdf(self._pdf_data_from_snapshot(record, await self._org_name()))
        await self._audit.append(
            actor=str(caller.employee_id),
            action="payroll.payslip.download",
            target=f"employee:{target}:{period}",
        )
        return pdf, f"payslip-{period}.pdf"

    async def live_payslip_pdf(
        self, caller: CurrentUser, employee_id: uuid.UUID | None, month: str | None
    ) -> tuple[bytes, str]:
        """Generate a payslip PDF from the LIVE computed slip — no finalize needed.
        Self-or-HR/Admin (my_slip enforces it): an employee generates their own; HR/
        Admin generate anyone's. Audited (rule 5.7)."""
        slip = await self.my_slip(caller, employee_id, month)  # authorizes + computes
        if slip.missing_compensation:
            raise ValidationError("No compensation on record for this employee.")
        target = employee_id or caller.employee_id
        employee = await self._employees.get(target)
        if employee is None:
            raise NotFoundError()
        year, m = _parse_month(slip.month)
        data = PayslipPdfData(
            org_name=await self._org_name(),
            employee_name=employee.full_name,
            job_title=employee.job_title,
            department=employee.department,
            location=employee.location,
            doj_label=employee.hire_date.strftime("%d %b %Y") if employee.hire_date else None,
            month_label=_month_label(year, m),
            currency=slip.currency,
            monthly_ctc_minor=slip.monthly_ctc_minor,
            monthly=slip.breakdown.model_dump(),
            prorated=slip.prorated.model_dump(),
            net_payable_minor=slip.net_minor,
            total_days=slip.total_days,
            working_days=slip.working_days,
            present_days=slip.present_days,
            paid_leave_days=slip.paid_leave_days,
            payable_days=slip.payable_days,
            generated_label=datetime.now(UTC).strftime("%d %b %Y"),
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="payroll.payslip.generate",
            target=f"employee:{target}:{slip.month}",
        )
        return render_payslip_pdf(data), f"payslip-{slip.month}.pdf"

    async def _release_from_estimate(
        self, est: PayrollEstimateRead, *, actor: CurrentUser | None
    ) -> tuple[int, int, int]:
        """Snapshot + email each employee with compensation. Returns
        (released, emailed, skipped). Email failures don't block release — the
        employee can still download from My Pay."""
        org_name = await self._org_name()
        year, m = _parse_month(est.month)
        label = _month_label(year, m)
        finalized_by = actor.employee_id if actor is not None else None
        released = emailed = skipped = 0
        for line in est.lines:
            if line.missing_compensation:
                skipped += 1
                continue
            snapshot = await self._payslips.upsert(
                snapshot=PayslipSnapshot(
                    employee_id=line.employee_id,
                    period_month=est.month,
                    employee_name=line.name,
                    department=line.department,
                    job_title=line.job_title,
                    location=line.location,
                    hire_date=line.hire_date,
                    currency=line.currency,
                    monthly_ctc_minor=line.monthly_ctc_minor,
                    gross_minor=line.breakdown.gross_minor,
                    net_minor=line.net_minor,
                    breakdown=line.breakdown.model_dump(),
                    prorated_breakdown=line.prorated.model_dump(),
                    total_days=line.total_days,
                    working_days=line.working_days,
                    present_days=line.present_days,
                    paid_leave_days=line.paid_leave_days,
                    payable_days=line.payable_days,
                    finalized_by=finalized_by,
                )
            )
            released += 1
            employee = await self._employees.get(line.employee_id)
            if employee is None or not employee.work_email:
                continue
            pdf = render_payslip_pdf(self._pdf_data_from_snapshot(snapshot, org_name))
            try:
                await self._email.send_payslip(
                    to=employee.work_email,
                    employee_name=line.name,
                    month_label=label,
                    currency=line.currency,
                    net_payable_minor=line.net_minor,
                    pdf=pdf,
                    pdf_filename=f"payslip-{est.month}.pdf",
                )
            except EmailError:
                continue
            await self._payslips.mark_emailed(snapshot)
            emailed += 1
        return released, emailed, skipped

    async def _org_name(self) -> str:
        org = await self._orgs.get()
        return org.name if org is not None else "Avora"

    def _pdf_data_from_snapshot(self, m: Payslip, org_name: str) -> PayslipPdfData:
        year, month = _parse_month(m.period_month)
        return PayslipPdfData(
            org_name=org_name,
            employee_name=m.employee_name,
            job_title=m.job_title,
            department=m.department,
            location=m.location,
            doj_label=m.hire_date.strftime("%d %b %Y") if m.hire_date else None,
            month_label=_month_label(year, month),
            currency=m.currency,
            monthly_ctc_minor=m.monthly_ctc_minor,
            monthly=m.breakdown,
            prorated=m.prorated_breakdown or m.breakdown,
            net_payable_minor=m.net_minor,
            total_days=m.total_days,
            working_days=m.working_days,
            present_days=m.present_days,
            paid_leave_days=m.paid_leave_days,
            payable_days=m.payable_days,
            generated_label=datetime.now(UTC).strftime("%d %b %Y"),
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
        """Scheduler entry point, once per month on pay day (idempotent): release
        every employee's payslip (snapshot + PDF email) AND send the HR digest."""
        spec = await self._policy.spec()
        period = month or self.current_month(spec.timezone)
        if await self._runs.get_for_month(period) is not None:
            return None  # already run this month
        est = await self.estimate(SYSTEM_CALLER, period)
        await self._release_from_estimate(est, actor=None)  # release + email employees
        return await self._send(SYSTEM_CALLER, period, source=PayrollRunSource.AUTO, actor=None)

    async def is_auto_release_due(self) -> bool:
        """True when auto-release is on and today (org tz) is the configured day."""
        s = await self.get_settings_model()
        if not s.auto_release_enabled:
            return False
        spec = await self._policy.spec()
        now_local = datetime.now(UTC).astimezone(ZoneInfo(spec.timezone))
        return now_local.day == s.auto_release_day

    async def release_for_system(self) -> PayrollRun | None:
        """Auto-release the PREVIOUS month's payslips on the configured day
        (idempotent): freeze + email everyone with compensation their slip. Skips a
        month that was already released — manually or on an earlier tick — so it
        never re-emails. The hands-off fallback to the manual "Release" click."""
        spec = await self._policy.spec()
        period = self.previous_month(spec.timezone)
        if await self._runs.get_for_month(period) is not None:
            return None  # already released
        est = await self.estimate(SYSTEM_CALLER, period)
        released, _emailed, _skipped = await self._release_from_estimate(est, actor=None)
        total_net = sum(line.net_minor for line in est.lines if not line.missing_compensation)
        return await self._runs.upsert(
            period_month=est.month,
            currency=est.currency,
            total_net_minor=total_net,
            employee_count=released,
            recipients="",
            source=PayrollRunSource.AUTO,
            triggered_by=None,
        )

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
                (line.name, line.net_minor, line.payable_days, line.total_days)
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
    async def _month_calendar(
        self, month: str, tz: str, working_days_per_week: int = 5
    ) -> _MonthCalendar:
        year, m = _parse_month(month)
        weekdays = weekdays_in_month(year, m, working_days_per_week)
        last_day = days_in_month(year, m)
        holidays = await self._holidays.dates_in_range(date(year, m, 1), date(year, m, last_day))
        working = frozenset(d for d in weekdays if d not in holidays)
        today = datetime.now(UTC).astimezone(ZoneInfo(tz)).date()
        elapsed = frozenset(d for d in working if d <= today)
        return _MonthCalendar(
            year=year,
            month=m,
            working_dates=working,
            elapsed_working_dates=elapsed,
            total_days=last_day,
            timezone=tz,
        )

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

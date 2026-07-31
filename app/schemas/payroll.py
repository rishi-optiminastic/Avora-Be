"""Payroll request/response schemas (Golden rule #5).

Money is integer minor units; the client formats for display. Recipient emails
are exposed as a list but stored comma-separated, so the *Read schemas build via
`from_model` rather than straight ORM attribute mapping.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.models.payroll_run import PayrollRun, PayrollRunSource
from app.models.payroll_settings import PayCycle, PayrollSettings
from app.models.payslip import Payslip
from app.schemas.common import ORMModel

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MINOR_MAX = 10**15


def parse_recipients(raw: str) -> list[str]:
    """Split a stored comma-separated recipient string into a clean list."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def clean_recipients(values: list[str]) -> list[str]:
    """Normalise, validate and de-dupe a recipient list (order preserved)."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        email = value.strip().lower()
        if not email:
            continue
        if not _EMAIL_RE.match(email):
            raise ValueError(f"'{value}' is not a valid email address")
        if email not in seen:
            seen.add(email)
            out.append(email)
    return out


class PayrollSettingsUpdate(BaseModel):
    """Full replace of the org payroll configuration (HR/Admin only)."""

    pay_day_of_month: int = Field(ge=1, le=28)
    currency: str = Field(min_length=3, max_length=3)
    pay_cycle: PayCycle = PayCycle.MONTHLY
    auto_send_enabled: bool = False
    auto_release_enabled: bool = False
    auto_release_day: int = Field(default=8, ge=1, le=28)
    recipients: list[str] = Field(default_factory=list, max_length=50)
    basic_pct: int = Field(ge=0, le=100)
    hra_pct: int = Field(ge=0, le=200)
    pf_pct: int = Field(ge=0, le=100)
    pf_cap_minor: int = Field(ge=0, le=_MINOR_MAX)
    professional_tax_minor: int = Field(ge=0, le=_MINOR_MAX)
    professional_tax_feb_minor: int = Field(ge=0, le=_MINOR_MAX)
    deduct_income_tax: bool = True

    @field_validator("recipients")
    @classmethod
    def _validate_recipients(cls, value: list[str]) -> list[str]:
        return clean_recipients(value)


class PayrollSettingsRead(BaseModel):
    pay_day_of_month: int
    currency: str
    pay_cycle: PayCycle
    auto_send_enabled: bool
    auto_release_enabled: bool
    auto_release_day: int
    recipients: list[str]
    basic_pct: int
    hra_pct: int
    pf_pct: int
    pf_cap_minor: int
    professional_tax_minor: int
    professional_tax_feb_minor: int
    deduct_income_tax: bool
    updated_at: datetime

    @classmethod
    def from_model(cls, model: PayrollSettings) -> PayrollSettingsRead:
        return cls(
            pay_day_of_month=model.pay_day_of_month,
            currency=model.currency,
            pay_cycle=model.pay_cycle,
            auto_send_enabled=model.auto_send_enabled,
            auto_release_enabled=model.auto_release_enabled,
            auto_release_day=model.auto_release_day,
            recipients=parse_recipients(model.recipients),
            basic_pct=model.basic_pct,
            hra_pct=model.hra_pct,
            pf_pct=model.pf_pct,
            pf_cap_minor=model.pf_cap_minor,
            professional_tax_minor=model.professional_tax_minor,
            professional_tax_feb_minor=model.professional_tax_feb_minor,
            deduct_income_tax=model.deduct_income_tax,
            updated_at=model.updated_at,
        )


class SalaryBreakdownRead(BaseModel):
    ctc_minor: int
    basic_minor: int
    hra_minor: int
    special_allowance_minor: int
    employer_pf_minor: int
    gross_minor: int
    employee_pf_minor: int
    professional_tax_minor: int
    income_tax_minor: int
    total_deduction_minor: int
    net_minor: int


class PayrollLineRead(BaseModel):
    """One employee's payroll for the month (full slip + attendance proration).

    `breakdown` is the full-month entitlement; `prorated` is the same slip scaled
    to `payable_days / total_days` (earnings + PF prorated, statutory taxes flat).
    `total_days` is calendar days in the month (the proration denominator);
    `working_days` is weekdays-minus-holidays and drives the present/absent split.
    """

    employee_id: uuid.UUID
    name: str
    department: str | None
    job_title: str | None
    location: str | None
    hire_date: date | None
    currency: str
    monthly_ctc_minor: int
    breakdown: SalaryBreakdownRead
    prorated: SalaryBreakdownRead
    total_days: int
    working_days: int
    # Working days elapsed so far (== working_days for a finished month). The
    # present/absent split is measured over this, so future days are not "absent".
    elapsed_working_days: int
    present_days: float
    paid_leave_days: float
    lop_days: float  # loss-of-pay days = elapsed working days neither present nor paid
    payable_days: float
    # Manual HR/Admin adjustments folded into net_minor (0 when none).
    adjustment_earnings_minor: int = 0
    adjustment_deductions_minor: int = 0
    net_minor: int  # the prorated take-home for the month
    missing_compensation: bool


class PayrollEstimateRead(BaseModel):
    month: str  # YYYY-MM
    currency: str
    total_days: int
    working_days: int
    employee_count: int
    total_ctc_minor: int
    total_net_minor: int
    lines: list[PayrollLineRead]


class PayslipRead(BaseModel):
    """One person's own monthly slip (the self-service 'My Pay' view).

    Same numbers as a single `PayrollLineRead`, surfaced on its own so an
    employee never sees anyone else's line. Money is in minor units.
    """

    month: str  # YYYY-MM
    currency: str
    monthly_ctc_minor: int
    breakdown: SalaryBreakdownRead
    prorated: SalaryBreakdownRead
    total_days: int
    working_days: int
    elapsed_working_days: int
    present_days: float
    paid_leave_days: float
    lop_days: float
    payable_days: float
    adjustment_earnings_minor: int = 0
    adjustment_deductions_minor: int = 0
    net_minor: int  # the prorated take-home for the month
    missing_compensation: bool


class PayslipSummaryRead(BaseModel):
    """One line in an employee's released-payslip history (the download list)."""

    month: str  # YYYY-MM
    currency: str
    net_payable_minor: int  # prorated take-home that was released
    gross_minor: int
    released_at: datetime
    emailed: bool

    @classmethod
    def from_model(cls, m: Payslip) -> PayslipSummaryRead:
        return cls(
            month=m.period_month,
            currency=m.currency,
            net_payable_minor=m.net_minor,
            gross_minor=m.gross_minor,
            released_at=m.released_at,
            emailed=m.emailed_at is not None,
        )


class ReleasedPayslipRead(BaseModel):
    """A finalized, HR-released payslip read back from its frozen snapshot.

    Distinct from the live `PayslipRead` (which recomputes): these numbers are
    locked, which is why employees only ever see months HR has released.
    """

    month: str  # YYYY-MM
    employee_id: uuid.UUID
    employee_name: str
    department: str | None
    job_title: str | None
    location: str | None
    hire_date: date | None
    currency: str
    monthly_ctc_minor: int
    breakdown: SalaryBreakdownRead
    prorated: SalaryBreakdownRead
    total_days: int
    working_days: int
    present_days: float
    paid_leave_days: float
    payable_days: float
    net_payable_minor: int  # prorated take-home
    released_at: datetime
    emailed: bool

    @classmethod
    def from_model(cls, m: Payslip) -> ReleasedPayslipRead:
        return cls(
            month=m.period_month,
            employee_id=m.employee_id,
            employee_name=m.employee_name,
            department=m.department,
            job_title=m.job_title,
            location=m.location,
            hire_date=m.hire_date,
            currency=m.currency,
            monthly_ctc_minor=m.monthly_ctc_minor,
            breakdown=SalaryBreakdownRead(**m.breakdown),
            prorated=SalaryBreakdownRead(**(m.prorated_breakdown or m.breakdown)),
            total_days=m.total_days,
            working_days=m.working_days,
            present_days=m.present_days,
            paid_leave_days=m.paid_leave_days,
            payable_days=m.payable_days,
            net_payable_minor=m.net_minor,
            released_at=m.released_at,
            emailed=m.emailed_at is not None,
        )


class PayrollFinalizeResult(BaseModel):
    """Outcome of HR finalizing (releasing) a month's payroll."""

    month: str  # YYYY-MM
    currency: str
    released_count: int
    emailed_count: int
    skipped_count: int  # employees skipped for missing compensation
    total_net_minor: int


class PayrollRunRead(ORMModel):
    period_month: str
    currency: str
    total_net_minor: int
    employee_count: int
    recipients: list[str]
    source: PayrollRunSource
    created_at: datetime

    @classmethod
    def from_model(cls, model: PayrollRun) -> PayrollRunRead:
        return cls(
            period_month=model.period_month,
            currency=model.currency,
            total_net_minor=model.total_net_minor,
            employee_count=model.employee_count,
            recipients=parse_recipients(model.recipients),
            source=model.source,
            created_at=model.created_at,
        )

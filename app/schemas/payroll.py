"""Payroll request/response schemas (Golden rule #5).

Money is integer minor units; the client formats for display. Recipient emails
are exposed as a list but stored comma-separated, so the *Read schemas build via
`from_model` rather than straight ORM attribute mapping.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.payroll_run import PayrollRun, PayrollRunSource
from app.models.payroll_settings import PayCycle, PayrollSettings
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
    recipients: list[str] = Field(default_factory=list, max_length=50)
    basic_pct: int = Field(ge=0, le=100)
    hra_pct: int = Field(ge=0, le=200)
    pf_pct: int = Field(ge=0, le=100)
    pf_cap_minor: int = Field(ge=0, le=_MINOR_MAX)
    professional_tax_minor: int = Field(ge=0, le=_MINOR_MAX)

    @field_validator("recipients")
    @classmethod
    def _validate_recipients(cls, value: list[str]) -> list[str]:
        return clean_recipients(value)


class PayrollSettingsRead(BaseModel):
    pay_day_of_month: int
    currency: str
    pay_cycle: PayCycle
    auto_send_enabled: bool
    recipients: list[str]
    basic_pct: int
    hra_pct: int
    pf_pct: int
    pf_cap_minor: int
    professional_tax_minor: int
    updated_at: datetime

    @classmethod
    def from_model(cls, model: PayrollSettings) -> PayrollSettingsRead:
        return cls(
            pay_day_of_month=model.pay_day_of_month,
            currency=model.currency,
            pay_cycle=model.pay_cycle,
            auto_send_enabled=model.auto_send_enabled,
            recipients=parse_recipients(model.recipients),
            basic_pct=model.basic_pct,
            hra_pct=model.hra_pct,
            pf_pct=model.pf_pct,
            pf_cap_minor=model.pf_cap_minor,
            professional_tax_minor=model.professional_tax_minor,
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
    total_deduction_minor: int
    net_minor: int


class PayrollLineRead(BaseModel):
    """One employee's payroll for the month (full slip + attendance proration)."""

    employee_id: uuid.UUID
    name: str
    department: str | None
    currency: str
    monthly_ctc_minor: int
    breakdown: SalaryBreakdownRead
    working_days: int
    present_days: float
    paid_leave_days: float
    payable_days: float
    net_minor: int  # the prorated take-home for the month
    missing_compensation: bool


class PayrollEstimateRead(BaseModel):
    month: str  # YYYY-MM
    currency: str
    working_days: int
    employee_count: int
    total_ctc_minor: int
    total_net_minor: int
    lines: list[PayrollLineRead]


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

"""Payroll salary maths — pure, deterministic, no I/O.

Given a monthly **CTC** (the stored compensation amount), derive the full Indian
salary slip — Basic / HRA / Special Allowance / employer & employee PF /
Professional Tax / Net — and prorate the net by attendance. Everything is in
integer **minor units** (paise/cents); we never do float money for the stored
amounts (only ratios). The defaults reproduce the reference structure exactly:

    CTC ₹50,000 → Basic 15,000 · HRA 7,500 · Special 25,700 · Gross 48,200
                  · PF 1,800 · Professional Tax 200 · Net 46,200
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class CalcConfig:
    """The org-tweakable knobs behind the slip (defaults = the reference image)."""

    basic_pct: int = 30  # Basic = 30% of CTC
    hra_pct: int = 50  # HRA = 50% of Basic
    pf_pct: int = 12  # PF = 12% of Basic …
    pf_cap_minor: int = 180_000  # … capped at ₹1,800 (statutory ceiling)
    professional_tax_minor: int = 20_000  # flat ₹200


@dataclass(frozen=True)
class SalaryBreakdown:
    """A full month's salary slip, every line in minor units."""

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


def compute_breakdown(monthly_ctc_minor: int, config: CalcConfig | None = None) -> SalaryBreakdown:
    """Derive the salary slip from a monthly CTC. Zero CTC → an all-zero slip."""
    cfg = config or CalcConfig()
    ctc = max(0, monthly_ctc_minor)
    if ctc == 0:
        return SalaryBreakdown(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    basic = round(ctc * cfg.basic_pct / 100)
    pf = min(round(basic * cfg.pf_pct / 100), cfg.pf_cap_minor)
    gross = ctc - pf  # CTC less employer PF
    hra = round(basic * cfg.hra_pct / 100)
    special = gross - basic - hra  # the balancing figure
    prof_tax = cfg.professional_tax_minor
    total_deduction = pf + prof_tax  # employee PF + professional tax
    net = gross - total_deduction
    return SalaryBreakdown(
        ctc_minor=ctc,
        basic_minor=basic,
        hra_minor=hra,
        special_allowance_minor=special,
        employer_pf_minor=pf,
        gross_minor=gross,
        employee_pf_minor=pf,
        professional_tax_minor=prof_tax,
        total_deduction_minor=total_deduction,
        net_minor=net,
    )


def monthly_ctc_minor(amount_minor: int, *, is_annual: bool) -> int:
    """Normalise a stored compensation amount to a monthly CTC."""
    return round(amount_minor / 12) if is_annual else amount_minor


def prorate_net(net_minor: int, payable_days: float, working_days: int) -> int:
    """Scale net by attendance: net * (payable / working), clamped to [0, net]."""
    if working_days <= 0:
        return net_minor
    ratio = min(1.0, max(0.0, payable_days / working_days))
    return round(net_minor * ratio)


def weekdays_in_month(year: int, month: int) -> list[date]:
    """Every Mon-Fri date in the given month (the working-day baseline)."""
    _, last = calendar.monthrange(year, month)
    return [
        d
        for day in range(1, last + 1)
        if (d := date(year, month, day)).weekday() < 5  # 0=Mon … 4=Fri
    ]


def working_days_between(start: date, end: date, holidays: set[date]) -> int:
    """Count Mon-Fri dates in [start, end] inclusive, minus the given holidays.

    The working-day baseline for leave accounting (mirrors payroll's
    weekdays-minus-holidays rule), so a Fri-Mon leave over a weekend is 2 days,
    not 4. Returns 0 when the range is inverted.
    """
    if end < start:
        return 0
    total = 0
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in holidays:
            total += 1
        current += timedelta(days=1)
    return total

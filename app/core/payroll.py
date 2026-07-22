"""Payroll salary maths — pure, deterministic, no I/O.

Given a monthly **CTC** (the stored compensation amount), derive the full Indian
salary slip — Basic / HRA / Special Allowance / employer & employee PF /
Professional Tax / income-tax TDS / Net — then prorate it to the days actually
paid: each earning and PF scales by ``payable_days / total_days`` (calendar days),
while Professional Tax and Income Tax stay flat as the statutory charges they are.
Everything is in integer **minor units** (paise/cents); we never do float money
for the stored amounts (only ratios). The defaults reproduce the reference
structure exactly:

    CTC ₹50,000 → Basic 15,000 · HRA 7,500 · Special 25,700 · Gross 48,200
                  · PF 1,800 · Professional Tax 200 · Net 46,200

Employee PF is `min(12% of Basic, ₹1,800)` — the statutory wage-ceiling rule —
computed automatically, not a manual per-slip choice. Professional tax follows
the common state pattern of a higher amount in February (₹300) and a flat
amount the other eleven months (₹200), both org-configurable. Income tax is
computed under the **New Regime** (FY 2026-27 slabs — Budget 2026 left Budget
2025's rates unchanged): a flat ₹75,000 standard deduction, then slabs, then
the Section 87A rebate that zeroes tax up to ₹12,00,000 taxable income (with
marginal relief just above it). It's estimated by annualising the monthly
gross (x 12) — a standard simplification for month-to-month TDS that assumes a
steady salary through the year, not a full YTD-cumulative recompute.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

_RUPEE = 100  # minor units (paise) per rupee

# New-regime standard deduction for salaried income (FY 2026-27).
_STANDARD_DEDUCTION_MINOR = 75_000 * _RUPEE
# Section 87A: taxable income at/below this is fully rebated (nil tax); above
# it, marginal relief caps tax at (taxable - this), so crossing the line never
# leaves you worse off than sitting exactly on it.
_REBATE_TAXABLE_LIMIT_MINOR = 12_00_000 * _RUPEE
# New-regime slabs: (lower bound, upper bound or None for no cap, rate %).
_TAX_SLABS_MINOR: tuple[tuple[int, int | None, int], ...] = (
    (0, 4_00_000 * _RUPEE, 0),
    (4_00_000 * _RUPEE, 8_00_000 * _RUPEE, 5),
    (8_00_000 * _RUPEE, 12_00_000 * _RUPEE, 10),
    (12_00_000 * _RUPEE, 16_00_000 * _RUPEE, 15),
    (16_00_000 * _RUPEE, 20_00_000 * _RUPEE, 20),
    (20_00_000 * _RUPEE, 24_00_000 * _RUPEE, 25),
    (24_00_000 * _RUPEE, None, 30),
)


def compute_annual_income_tax(annual_gross_minor: int) -> int:
    """New-regime annual TDS from annual gross salary, in minor units.

    Taxable income = gross - standard deduction (HRA/PF aren't exempt under
    the new regime, so nothing else is subtracted). Nil up to ₹12L taxable via
    the 87A rebate; slab tax with marginal relief above it.
    """
    taxable = max(0, annual_gross_minor - _STANDARD_DEDUCTION_MINOR)
    if taxable <= _REBATE_TAXABLE_LIMIT_MINOR:
        return 0
    tax = 0
    for lower, upper, rate in _TAX_SLABS_MINOR:
        if taxable <= lower:
            break
        band_top = min(taxable, upper) if upper is not None else taxable
        tax += round((band_top - lower) * rate / 100)
    return min(tax, taxable - _REBATE_TAXABLE_LIMIT_MINOR)


@dataclass(frozen=True)
class CalcConfig:
    """The org-tweakable knobs behind the slip (defaults = the reference image)."""

    basic_pct: int = 30  # Basic = 30% of CTC
    hra_pct: int = 50  # HRA = 50% of Basic
    pf_pct: int = 12  # PF = 12% of Basic …
    pf_cap_minor: int = 180_000  # … capped at ₹1,800 (statutory ceiling)
    professional_tax_minor: int = 20_000  # flat ₹200, most months
    professional_tax_feb_minor: int = 30_000  # ₹300 in February
    deduct_income_tax: bool = True  # withhold estimated new-regime TDS monthly


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
    income_tax_minor: int
    total_deduction_minor: int
    net_minor: int


def compute_breakdown(
    monthly_ctc_minor: int,
    config: CalcConfig | None = None,
    *,
    month: int | None = None,
) -> SalaryBreakdown:
    """Derive the salary slip from a monthly CTC. Zero CTC → an all-zero slip.

    `month` (1-12) picks the February professional-tax amount when it's 2;
    omit it (or pass any other month) to use the regular amount.
    """
    cfg = config or CalcConfig()
    ctc = max(0, monthly_ctc_minor)
    if ctc == 0:
        return SalaryBreakdown(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    basic = round(ctc * cfg.basic_pct / 100)
    pf = min(round(basic * cfg.pf_pct / 100), cfg.pf_cap_minor)
    gross = ctc - pf  # CTC less employer PF
    hra = round(basic * cfg.hra_pct / 100)
    special = gross - basic - hra  # the balancing figure
    prof_tax = cfg.professional_tax_feb_minor if month == 2 else cfg.professional_tax_minor
    income_tax = round(compute_annual_income_tax(gross * 12) / 12) if cfg.deduct_income_tax else 0
    total_deduction = pf + prof_tax + income_tax
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
        income_tax_minor=income_tax,
        total_deduction_minor=total_deduction,
        net_minor=net,
    )


def monthly_ctc_minor(amount_minor: int, *, is_annual: bool) -> int:
    """Normalise a stored compensation amount to a monthly CTC."""
    return round(amount_minor / 12) if is_annual else amount_minor


def days_in_month(year: int, month: int) -> int:
    """Total calendar days in the month — the proration denominator."""
    return calendar.monthrange(year, month)[1]


def attendance_ratio(payable_days: float, total_days: int) -> float:
    """Payable share of the month, clamped to [0, 1]. Full month if no days."""
    if total_days <= 0:
        return 1.0
    return min(1.0, max(0.0, payable_days / total_days))


def prorate_breakdown(
    breakdown: SalaryBreakdown,
    payable_days: float,
    total_days: int,
    config: CalcConfig | None = None,
) -> SalaryBreakdown:
    """Prorate a full-month slip to the days actually paid (the reference method).

    Every *earning* (Basic / HRA / Special / Gross / CTC) scales by
    ``payable_days / total_days``. **PF is recomputed from the reduced Basic** —
    ``min(pf_pct% of prorated Basic, pf_cap)`` — not shrunk proportionally, so a
    high earner already at the cap (₹1,800) stays pinned there, not dropping.
    Professional Tax and Income Tax are flat statutory monthly charges and are
    **not** prorated. Net is rebuilt from the prorated lines. Reproduces the
    reference sheet exactly:

        CTC ₹40,000, 26 of 30 days → Gross 33,419 · PF 1,248 · PT 200 (flat)
                                     → Net 31,971
    """
    cfg = config or CalcConfig()
    ratio = attendance_ratio(payable_days, total_days)

    def scale(minor: int) -> int:
        return round(minor * ratio)

    basic = scale(breakdown.basic_minor)
    # PF tracks the reduced Basic (12% of prorated Basic, capped) — a fresh
    # computation, not a proportional shrink of the full-month PF figure.
    pf = min(round(basic * cfg.pf_pct / 100), cfg.pf_cap_minor)
    prof_tax = breakdown.professional_tax_minor  # flat statutory charge
    income_tax = breakdown.income_tax_minor  # flat monthly withholding
    total_deduction = pf + prof_tax + income_tax
    gross = scale(breakdown.gross_minor)
    return SalaryBreakdown(
        ctc_minor=scale(breakdown.ctc_minor),
        basic_minor=basic,
        hra_minor=scale(breakdown.hra_minor),
        special_allowance_minor=scale(breakdown.special_allowance_minor),
        employer_pf_minor=pf,
        gross_minor=gross,
        employee_pf_minor=pf,
        professional_tax_minor=prof_tax,
        income_tax_minor=income_tax,
        total_deduction_minor=total_deduction,
        net_minor=gross - total_deduction,
    )


def weekdays_in_month(year: int, month: int, working_days_per_week: int = 5) -> list[date]:
    """Every working-day date in the given month (the working-day baseline).

    `working_days_per_week` is counted from Monday: 5 ⇒ Mon-Fri, 6 ⇒ Mon-Sat,
    7 ⇒ every day. `weekday()` is 0=Mon … 6=Sun, so `< working_days_per_week`
    selects the first N days of the week.
    """
    _, last = calendar.monthrange(year, month)
    return [
        d
        for day in range(1, last + 1)
        if (d := date(year, month, day)).weekday() < working_days_per_week
    ]


def working_days_between(
    start: date, end: date, holidays: set[date], working_days_per_week: int = 5
) -> int:
    """Count working-day dates in [start, end] inclusive, minus the given holidays.

    The working-day baseline for leave accounting (mirrors payroll's
    working-days-minus-holidays rule), so a Fri-Mon leave over a weekend is 2 days,
    not 4. `working_days_per_week` is counted from Monday (5 ⇒ Mon-Fri). Returns 0
    when the range is inverted.
    """
    if end < start:
        return 0
    total = 0
    current = start
    while current <= end:
        if current.weekday() < working_days_per_week and current not in holidays:
            total += 1
        current += timedelta(days=1)
    return total

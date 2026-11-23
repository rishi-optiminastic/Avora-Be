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

Employee PF is 12% of Basic up to a ₹25,000 Basic ceiling, so the deduction
stops at ₹3,000 a month - computed automatically, not a manual per-slip choice.
Professional tax is ₹200 a month and ₹300 in February, both org-configurable,
with one exemption: a female employee whose monthly gross is ₹25,000 or less
pays no professional tax at all (the state slab exempts her). Income tax is
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


# Provident Fund ceiling: PF is 12% of Basic, but Basic counts only up to
# ₹25,000 a month, so the deduction stops at ₹3,000 (12% of ₹25,000).
PF_CAP_MINOR = 3_000_00

# The gender whose professional-tax exemption slab applies. State PT slabs exempt
# women below a monthly-salary threshold; men have no equivalent exemption at
# these income levels. `Gender` is a `StrEnum`, so a caller passes the model value
# straight in and this module stays free of any `app.models` import.
PT_EXEMPT_GENDER = "female"


@dataclass(frozen=True)
class CalcConfig:
    """The org-tweakable knobs behind the slip (defaults = the reference image)."""

    basic_pct: int = 30  # Basic = 30% of CTC
    hra_pct: int = 50  # HRA = 50% of Basic
    pf_pct: int = 12  # PF = 12% of Basic …
    # … up to a ₹25,000 Basic ceiling, which 12% turns into a ₹3,000 cap. Capping
    # the AMOUNT rather than the wage is the same rule at 12% and survives a
    # change to `pf_pct` without silently re-deriving someone's ceiling.
    pf_cap_minor: int = PF_CAP_MINOR
    professional_tax_minor: int = 20_000  # flat ₹200, most months
    professional_tax_feb_minor: int = 30_000  # ₹300 in February
    # Monthly gross at or below which an exempt-category employee owes no
    # professional tax. Not an org Settings knob: it is a state slab boundary,
    # kept here beside the amounts it overrides so the rule reads in one place.
    professional_tax_exempt_upto_minor: int = 25_000_00
    deduct_income_tax: bool = True  # withhold estimated new-regime TDS monthly
    # Whether Provident Fund applies at all. Off means the employer share is not
    # carved out of CTC (so gross == CTC) AND nothing is deducted, which is why
    # take-home rises by about twice the PF figure rather than once.
    deduct_pf: bool = True


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


def professional_tax_for(
    monthly_gross_minor: int,
    config: CalcConfig | None = None,
    *,
    month: int | None = None,
    gender: str | None = None,
) -> int:
    """One month's professional tax, in minor units.

    The configured amount (₹200, or ₹300 in February), except that a female
    employee earning ₹25,000 or less a month owes nothing.

    The threshold is read against the FULL-MONTH gross, never the prorated one:
    professional tax is a charge on the salary the contract pays, so a month of
    unpaid leave must not duck someone under the line and refund her ₹200.
    """
    cfg = config or CalcConfig()
    exempt_band = (gender or "").strip().lower() == PT_EXEMPT_GENDER
    if exempt_band and monthly_gross_minor <= cfg.professional_tax_exempt_upto_minor:
        return 0
    return cfg.professional_tax_feb_minor if month == 2 else cfg.professional_tax_minor


def compute_breakdown(
    monthly_ctc_minor: int,
    config: CalcConfig | None = None,
    *,
    month: int | None = None,
    gender: str | None = None,
) -> SalaryBreakdown:
    """Derive the salary slip from a monthly CTC. Zero CTC → an all-zero slip.

    `month` (1-12) picks the February professional-tax amount when it's 2;
    omit it (or pass any other month) to use the regular amount. `gender` picks
    the professional-tax exemption band (see `professional_tax_for`); omitting it
    charges the unexempted amount, so a missing gender can never under-deduct.
    """
    cfg = config or CalcConfig()
    ctc = max(0, monthly_ctc_minor)
    if ctc == 0:
        return SalaryBreakdown(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    basic = round(ctc * cfg.basic_pct / 100)
    pf = min(round(basic * cfg.pf_pct / 100), cfg.pf_cap_minor) if cfg.deduct_pf else 0
    gross = ctc - pf  # CTC less employer PF (== CTC when PF does not apply)
    hra = round(basic * cfg.hra_pct / 100)
    special = gross - basic - hra  # the balancing figure
    prof_tax = professional_tax_for(gross, cfg, month=month, gender=gender)
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


# The EPS/EDLI pensionable-wage ceiling (₹15,000/month) and the EPS employer cap
# (₹1,250). Deliberately NOT `PF_CAP_MINOR`: the 12% PF deduction counts Basic up
# to ₹25,000, but the pension and insurance slices of the employer's half are
# still legislated against ₹15,000, so raising the PF ceiling moves the EPF
# remainder (employee PF minus EPS) and leaves EPS and EDLI where they were.
#
# Public because the payroll register documents these rates on its Formulas
# sheet. Restating them there as literals would let the sheet drift away from
# the arithmetic it claims to explain.
PF_WAGE_CEILING_MINOR = 15_000_00
EPS_CAP_MINOR = 1_250_00
EPS_RATE_PCT = 8.33  # employer pension share, of the capped wage
EDLI_RATE_PCT = 0.5  # employer insurance share, of the capped wage
EPF_ADMIN_RATE_PCT = 0.5  # EPF administration charges, of the FULL Basic


@dataclass(frozen=True)
class EmployerContributions:
    """The employer's statutory PF-side costs for a month, split the way an Indian
    payroll register reports them. All derived from (already-prorated) Basic."""

    epf_employer_minor: int  # the remainder: employee PF minus EPS
    eps_minor: int  # EPS_RATE_PCT of min(Basic, ceiling), capped at ₹1,250
    edli_minor: int  # EDLI_RATE_PCT of min(Basic, ceiling)
    admin_minor: int  # EPF_ADMIN_RATE_PCT of Basic (EPF admin charges)
    total_minor: int


def employer_contributions(basic_minor: int, employee_pf_minor: int) -> EmployerContributions:
    """Split the employer's 12% PF into EPS / EPF / EDLI / admin, from Basic.

    `employee_pf_minor` is the (prorated) employee PF for the same period; the
    employer EPF share is that minus EPS, so the two PF halves always agree.
    """
    basic = max(0, basic_minor)
    wage = min(basic, PF_WAGE_CEILING_MINOR)
    # EPS is 8.33% of the wage, but a flat ₹1,250 at/above the ceiling (the
    # statutory max — 8.33% of ₹15,000 is ₹1,249.50, reported as ₹1,250).
    eps = EPS_CAP_MINOR if basic >= PF_WAGE_CEILING_MINOR else round(wage * EPS_RATE_PCT / 100)
    epf_employer = max(0, employee_pf_minor - eps)
    edli = round(wage * EDLI_RATE_PCT / 100)
    admin = round(basic * EPF_ADMIN_RATE_PCT / 100)
    return EmployerContributions(
        epf_employer_minor=epf_employer,
        eps_minor=eps,
        edli_minor=edli,
        admin_minor=admin,
        total_minor=epf_employer + eps + edli + admin,
    )


def days_in_month(year: int, month: int) -> int:
    """Total calendar days in the month — the proration denominator."""
    return calendar.monthrange(year, month)[1]


def attendance_ratio(payable_days: float, total_days: int) -> float:
    """Payable share of the month, clamped to [0, 1]. Full month if no days."""
    if total_days <= 0:
        return 1.0
    return min(1.0, max(0.0, payable_days / total_days))


def prorate_breakdown(
    breakdown: SalaryBreakdown, payable_days: float, total_days: int
) -> SalaryBreakdown:
    """Prorate a full-month slip to the days actually paid (the reference method).

    Every *earning* (Basic / HRA / Special / Gross / CTC) and *both* PF figures
    scale by ``payable_days / total_days``; Professional Tax and Income Tax are
    flat statutory monthly charges and are **not** prorated. Net is rebuilt from
    the prorated lines, so it equals prorated-gross minus prorated-employee-PF
    minus the flat statutory deductions. Reproduces the reference sheet exactly:

        CTC ₹40,000, 26 of 30 days → Gross 33,419 · PF 1,248 · PT 200 (flat)
                                     → Net 31,971
    """
    ratio = attendance_ratio(payable_days, total_days)
    if ratio == 0:
        # Nobody paid for any day this month owes a statutory deduction either.
        # Professional tax is flat BECAUSE it is a charge on salary, so leaving it
        # standing against a zero gross produced a NEGATIVE net pay on the register
        # (an employee who had not started yet was billed 200 rupees).
        return SalaryBreakdown(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    def scale(minor: int) -> int:
        return round(minor * ratio)

    employee_pf = scale(breakdown.employee_pf_minor)
    prof_tax = breakdown.professional_tax_minor  # flat statutory charge
    income_tax = breakdown.income_tax_minor  # flat monthly withholding
    total_deduction = employee_pf + prof_tax + income_tax
    gross = scale(breakdown.gross_minor)
    return SalaryBreakdown(
        ctc_minor=scale(breakdown.ctc_minor),
        basic_minor=scale(breakdown.basic_minor),
        hra_minor=scale(breakdown.hra_minor),
        special_allowance_minor=scale(breakdown.special_allowance_minor),
        employer_pf_minor=scale(breakdown.employer_pf_minor),
        gross_minor=gross,
        employee_pf_minor=employee_pf,
        professional_tax_minor=prof_tax,
        income_tax_minor=income_tax,
        total_deduction_minor=total_deduction,
        net_minor=gross - total_deduction,
    )


def payroll_window(year: int, month: int, hire_date: date | None = None) -> tuple[date, date]:
    """The span of the month an employee is actually on the payroll for.

    Normally the whole month. In the month someone JOINS, it starts on their
    hire date — they are not owed (and must not be charged loss-of-pay for) the
    days before they existed to us. Weekends are the reason this matters: LOP is
    only ever charged on working days, so without this window the Saturdays and
    Sundays sitting before a mid-month joining date were silently paid.

    A hire date in a later month yields an inverted range (start > end), which
    `payable_base_days` reports as zero days.
    """
    first = date(year, month, 1)
    last = date(year, month, days_in_month(year, month))
    if hire_date is not None and hire_date > first:
        return hire_date, last
    return first, last


def payable_base_days(year: int, month: int, hire_date: date | None = None) -> int:
    """Calendar days the employee is on the payroll this month — the ceiling on
    payable days, and the base loss-of-pay is subtracted from.

    A full month for everyone already on the roster; for a mid-month joiner, only
    the days from their hire date onward (15 Aug ⇒ 17 of August's 31).
    """
    start, end = payroll_window(year, month, hire_date)
    return (end - start).days + 1 if end >= start else 0


_SATURDAY = 5
# Saturdays that are weekly offs, counted within the calendar month: the 2nd and
# the 4th (Attendance Guidelines 2026 — "1st and 3rd Saturdays will be working
# days, while 2nd and 4th Saturdays will be weekly offs"). A 5th Saturday is not
# named by the policy, so it stays a working day like the 1st and 3rd.
_SATURDAYS_OFF = (2, 4)

# Leave & Attendance Policy 2026 is "effective 7th September 2026". Rules that
# change how a PAST day is classified must not reach back before it: turning the
# 2nd and 4th Saturdays into weekly offs retroactively would restate attendance
# records people have already seen and shift payroll months already processed.
# Every day before this date keeps the rule it was actually worked under.
POLICY_EFFECTIVE_DATE = date(2026, 9, 7)


def is_working_saturday(day: date) -> bool:
    """Whether this Saturday is worked. The 1st, 3rd (and any 5th) are; the 2nd
    and 4th are weekly offs."""
    return ((day.day - 1) // 7 + 1) not in _SATURDAYS_OFF


def is_working_day(day: date, working_days_per_week: int = 5) -> bool:
    """Whether the company works on `day`, ignoring holidays.

    THE single answer to that question. Attendance, monitoring and payroll each
    used to test `weekday() < working_days_per_week` for themselves, which meant
    adding the alternate-Saturday rule in one place would have left the others
    marking people absent on their own weekly off.

    The alternate-Saturday rule applies only from `POLICY_EFFECTIVE_DATE`. Before
    it, every Mon-Sat day was worked and was recorded that way, so re-answering
    for those days would rewrite attendance and pay that are already settled.
    """
    if day.weekday() >= working_days_per_week:
        return False
    if working_days_per_week == 6 and day.weekday() == _SATURDAY and day >= POLICY_EFFECTIVE_DATE:
        return is_working_saturday(day)
    return True


def weekdays_in_month(year: int, month: int, working_days_per_week: int = 5) -> list[date]:
    """Every working-day date in the given month (the working-day baseline).

    `working_days_per_week` is counted from Monday: 5 ⇒ Mon-Fri, 6 ⇒ Mon-Sat,
    7 ⇒ every day. `weekday()` is 0=Mon … 6=Sun, so `< working_days_per_week`
    selects the first N days of the week.

    A six-day week is Mon-Sat with the 2nd and 4th Saturday off, which is the
    actual shift pattern — treating every Saturday as worked overstated the
    month by two days and made those two Saturdays dockable as absence.
    """
    _, last = calendar.monthrange(year, month)
    return [
        d
        for day in range(1, last + 1)
        if is_working_day(d := date(year, month, day), working_days_per_week)
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
        if is_working_day(current, working_days_per_week) and current not in holidays:
            total += 1
        current += timedelta(days=1)
    return total

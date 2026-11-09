"""Access spend maths - pure, no I/O.

Turns the cost on each tech asset into what finance asks for: what it costs a
month, what it costs a year, and where that money goes (by team, category and
kind). Kept apart from the service so the screen and the spreadsheet export
are built from the same numbers and can never disagree.

Rules:

* A cost is read against its billing cycle. Monthly x 12, Quarterly x 4,
  Yearly x 1 a year. Usage-based bills are entered as a typical month, so they
  count as monthly and are flagged as estimates. One-time and Free bills are
  listed but never recur.
* Only live spend is totalled: Active and Trial. Paused, Cancelled and Expired
  rows stay in the line items, so finance can see what was stopped.
* Money is integer minor units and never converted between currencies here:
  rupees and dollars are totalled separately, because a silent exchange rate
  is a wrong number nobody can trace.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

UNASSIGNED_TEAM = "Unassigned"
NO_BRAND = "No brand"
UNCATEGORISED = "Uncategorised"

# Months covered by one bill of each cycle; None = does not recur.
_CYCLE_MONTHS: dict[str, int | None] = {
    "monthly": 1,
    "quarterly": 3,
    "half-yearly": 6,
    "yearly": 12,
    "annual": 12,
    "annually": 12,
    "usage-based": 1,
    "one-time": None,
    "free": None,
}
_ESTIMATED_CYCLES = frozenset({"usage-based"})
_LIVE_STATUSES = frozenset({"active", "trial"})
_MONTHS_PER_YEAR = 12


@dataclass(frozen=True)
class SpendInput:
    """One asset's cost, as the service reads it from the row."""

    asset_id: str
    name: str
    kind: str
    category: str | None
    team: str | None
    brand: str | None
    owner_name: str | None
    billed_to: str | None
    status: str
    cost_minor: int
    currency: str
    billing_cycle: str | None
    renews_on: date | None
    payment_method: str | None
    card_last4: str | None
    auto_renew: str | None


@dataclass(frozen=True)
class SpendLine:
    item: SpendInput
    team: str
    brand: str
    category: str
    monthly_minor: int  # 0 when it does not recur
    annual_minor: int
    recurring: bool
    estimated: bool
    counted: bool  # included in totals (live and recurring)


@dataclass(frozen=True)
class SpendTotals:
    """Monthly and annual spend per currency, minor units."""

    monthly: dict[str, int]
    annual: dict[str, int]
    count: int


def cycle_months(cycle: str | None) -> int | None:
    """Months one bill covers, or None when the bill does not recur. A blank or
    unknown cycle is treated as monthly, the commonest case, and flagged later."""
    if not cycle or not cycle.strip():
        return 1
    return _CYCLE_MONTHS.get(cycle.strip().lower(), 1)


def to_line(item: SpendInput) -> SpendLine:
    months = cycle_months(item.billing_cycle)
    recurring = months is not None
    monthly = round(item.cost_minor / months) if months else 0
    annual = round(item.cost_minor * _MONTHS_PER_YEAR / months) if months else 0
    cycle = (item.billing_cycle or "").strip().lower()
    return SpendLine(
        item=item,
        team=(item.team or "").strip() or UNASSIGNED_TEAM,
        brand=(item.brand or "").strip() or NO_BRAND,
        category=(item.category or "").strip() or UNCATEGORISED,
        monthly_minor=monthly,
        annual_minor=annual,
        recurring=recurring,
        estimated=cycle in _ESTIMATED_CYCLES or cycle not in _CYCLE_MONTHS,
        counted=recurring and item.status.lower() in _LIVE_STATUSES,
    )


def totals(lines: Iterable[SpendLine]) -> SpendTotals:
    monthly: dict[str, int] = defaultdict(int)
    annual: dict[str, int] = defaultdict(int)
    count = 0
    for line in lines:
        if not line.counted:
            continue
        monthly[line.item.currency] += line.monthly_minor
        annual[line.item.currency] += line.annual_minor
        count += 1
    return SpendTotals(monthly=dict(monthly), annual=dict(annual), count=count)


def group_totals(lines: Sequence[SpendLine], key: str) -> list[tuple[str, SpendTotals]]:
    """Totals per `brand`, `team`, `category` or `kind`, biggest rupee spend first."""
    groups: dict[str, list[SpendLine]] = defaultdict(list)
    for line in lines:
        label = line.item.kind if key == "kind" else getattr(line, key)
        groups[label].append(line)
    out = [(label, totals(group)) for label, group in groups.items()]
    out = [(label, t) for label, t in out if t.count > 0]
    return sorted(out, key=lambda pair: (-sum(pair[1].monthly.values()), pair[0]))


def renewals_within(lines: Sequence[SpendLine], today: date, days: int) -> list[SpendLine]:
    """Live, recurring lines that renew in the next `days` days, soonest first."""
    due = [
        line
        for line in lines
        if line.counted
        and line.item.renews_on is not None
        and 0 <= (line.item.renews_on - today).days <= days
    ]
    return sorted(due, key=lambda line: line.item.renews_on or today)

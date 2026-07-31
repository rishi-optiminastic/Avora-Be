"""Derived attendance + live-activity response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from app.core.categories import ProductivityCategory


class AttendanceStatus(StrEnum):
    FULL_DAY = "full_day"  # on time (or regularized) + enough hours
    HALF_DAY = "half_day"  # too late (past the reg window) or too few hours
    LATE = "late"  # arrived in the regularization window, not yet regularized
    ABSENT = "absent"
    ON_LEAVE = "on_leave"  # a working day covered by approved paid leave
    PRESENT = "present"  # on time / regularized, day still in progress (also legacy fallback)


class AttendanceRead(BaseModel):
    """One employee's derived attendance for a day."""

    employee_id: uuid.UUID
    status: AttendanceStatus
    login_at: datetime | None
    logout_at: datetime | None
    worked_minutes: int
    idle_minutes: int
    active_minutes: int
    productivity_pct: int
    # Payroll-relevant flags (spec: late login / early logout / missed logout).
    late_login: bool = False
    early_logout: bool = False
    missed_logout: bool = False
    regularizable: bool = False  # late within the window → can request regularization
    regularized: bool = False  # an approved regularization upgraded this day
    ip_address: str | None = None
    # Where the clock-in / clock-out came from: "biometric", "dashboard" (the app),
    # "agent" (activity fallback), or "auto" (auto-checkout). None when not set.
    clock_in_source: str | None = None
    clock_out_source: str | None = None


class ActivityNowRead(BaseModel):
    """One employee's current live state (from their latest recent sample)."""

    employee_id: uuid.UUID
    online: bool
    idle: bool
    active_window: str | None
    idle_seconds: int
    last_seen_at: datetime | None


class DomainStat(BaseModel):
    """Time on a single domain, with its productivity category."""

    domain: str
    category: ProductivityCategory
    minutes: int
    flagged: bool = False  # unproductive / requires-review ⇒ surfaced for managers


class BrowsingRead(BaseModel):
    """One employee's derived browsing breakdown for a day."""

    employee_id: uuid.UUID
    total_minutes: int
    productive_minutes: int
    neutral_minutes: int
    unproductive_minutes: int
    focus_pct: int
    top_domains: list[DomainStat]


class InsightRead(BaseModel):
    """One employee's productivity rollup (today) + 7-day focus trend (Phase 4)."""

    employee_id: uuid.UUID
    focus_pct: int
    productive_minutes: int
    neutral_minutes: int
    unproductive_minutes: int
    total_minutes: int
    at_risk: bool
    trend: list[int]  # daily focus % oldest→newest

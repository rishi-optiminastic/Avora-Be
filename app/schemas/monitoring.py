"""Derived attendance + live-activity response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from app.core.categories import ProductivityCategory


class AttendanceStatus(StrEnum):
    PRESENT = "present"
    LATE = "late"
    ABSENT = "absent"


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


class BrowsingRead(BaseModel):
    """One employee's derived browsing breakdown for a day."""

    employee_id: uuid.UUID
    total_minutes: int
    productive_minutes: int
    neutral_minutes: int
    distracting_minutes: int
    focus_pct: int
    top_domains: list[DomainStat]


class InsightRead(BaseModel):
    """One employee's productivity rollup (today) + 7-day focus trend (Phase 4)."""

    employee_id: uuid.UUID
    focus_pct: int
    productive_minutes: int
    neutral_minutes: int
    distracting_minutes: int
    total_minutes: int
    at_risk: bool
    trend: list[int]  # daily focus % oldest→newest

"""Leave request/response schemas (Golden rule #5 — never return the ORM model)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from app.core.tenure import TenureStatus
from app.models.leave import HalfDayPeriod, LeaveStatus, LeaveType
from app.schemas.common import ORMModel


class LeaveCreate(BaseModel):
    """An employee applies for leave (for themselves)."""

    leave_type: LeaveType
    start_date: datetime
    end_date: datetime
    half_day_period: HalfDayPeriod | None = None
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _check_range(self) -> LeaveCreate:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.leave_type is LeaveType.HALF_DAY:
            if self.half_day_period is None:
                raise ValueError("half_day_period is required for a half-day leave")
            if self.start_date.date() != self.end_date.date():
                raise ValueError("A half-day leave must start and end on the same day")
        elif self.half_day_period is not None:
            raise ValueError("half_day_period is only valid for a half-day leave")
        return self


class LeaveDecision(BaseModel):
    """A reviewer approves or rejects a submitted request."""

    approve: bool
    note: str | None = Field(default=None, max_length=1000)


class LeaveRead(ORMModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    leave_type: LeaveType
    half_day_period: HalfDayPeriod | None
    start_date: datetime
    end_date: datetime
    reason: str | None
    status: LeaveStatus
    reviewer_id: uuid.UUID | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime
    updated_at: datetime
    # Populated on list responses so the UI can badge the thread count.
    comment_count: int = 0


class TierQuotaRead(BaseModel):
    """One tenure band's entitlement for one leave type."""

    tier: TenureStatus
    leave_type: LeaveType
    annual_days: int | None
    monthly_accrual_days: float | None


class LeaveTypeBalance(BaseModel):
    """The entitlement picture for one paid leave type, in working days."""

    leave_type: LeaveType
    allocated: float  # annual quota from the leave policy
    used: float  # approved days in the current leave year
    pending: float  # submitted-but-undecided days in the current leave year
    remaining: float  # allocated - used - pending (may be negative if over-quota)


class LeaveBalanceRead(BaseModel):
    """An employee's leave balances for their current (joining-anniversary) year."""

    leave_year_start: date
    leave_year_end: date
    balances: list[LeaveTypeBalance]
    # Which entitlement band the employee is in right now, and the date they
    # leave (or left) probation — so the UI can explain WHY a quota is what it is
    # instead of showing an unexplained number.
    tenure_status: TenureStatus
    probation_end_date: date
    # Leave types whose allocation accrues monthly rather than being granted up
    # front, so the UI can label them "1/month, carries forward".
    accruing_types: list[LeaveType] = Field(default_factory=list)


class LeaveCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class LeaveCommentRead(ORMModel):
    id: uuid.UUID
    leave_id: uuid.UUID
    author_id: uuid.UUID | None
    body: str
    created_at: datetime

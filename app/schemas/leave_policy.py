"""Leave-policy schemas (Golden rule #5 — never return the ORM model)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.leave_policy import LeavePolicy


class LeavePolicyRead(BaseModel):
    annual_planned_days: int
    annual_sick_days: int
    updated_at: datetime

    @classmethod
    def from_model(cls, m: LeavePolicy) -> LeavePolicyRead:
        return cls(
            annual_planned_days=m.annual_planned_days,
            annual_sick_days=m.annual_sick_days,
            updated_at=m.updated_at,
        )


class LeavePolicyUpdate(BaseModel):
    """Partial update of the org leave entitlement (HR/Admin only)."""

    annual_planned_days: int | None = Field(default=None, ge=0, le=365)
    annual_sick_days: int | None = Field(default=None, ge=0, le=365)

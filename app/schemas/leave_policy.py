"""Leave-policy schemas (Golden rule #5 — never return the ORM model)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.leave_policy import LeavePolicy


class LeavePolicyRead(BaseModel):
    annual_planned_days: int
    annual_sick_days: int
    annual_days: int
    bereavement_days: int
    birthday_days: int
    maternity_days: int
    paternity_days: int
    marriage_days: int
    planned_min_notice_days: int
    max_backdate_days: int
    probation_months: int
    updated_at: datetime

    @classmethod
    def from_model(cls, m: LeavePolicy) -> LeavePolicyRead:
        return cls(
            annual_planned_days=m.annual_planned_days,
            annual_sick_days=m.annual_sick_days,
            annual_days=m.annual_days,
            bereavement_days=m.bereavement_days,
            birthday_days=m.birthday_days,
            maternity_days=m.maternity_days,
            paternity_days=m.paternity_days,
            marriage_days=m.marriage_days,
            planned_min_notice_days=m.planned_min_notice_days,
            max_backdate_days=m.max_backdate_days,
            probation_months=m.probation_months,
            updated_at=m.updated_at,
        )


class LeavePolicyUpdate(BaseModel):
    """Partial update of the org leave entitlement (HR/Admin only)."""

    annual_planned_days: int | None = Field(default=None, ge=0, le=365)
    annual_sick_days: int | None = Field(default=None, ge=0, le=365)
    annual_days: int | None = Field(default=None, ge=0, le=365)
    bereavement_days: int | None = Field(default=None, ge=0, le=365)
    birthday_days: int | None = Field(default=None, ge=0, le=365)
    maternity_days: int | None = Field(default=None, ge=0, le=365)
    paternity_days: int | None = Field(default=None, ge=0, le=365)
    marriage_days: int | None = Field(default=None, ge=0, le=365)
    planned_min_notice_days: int | None = Field(default=None, ge=0, le=90)
    max_backdate_days: int | None = Field(default=None, ge=0, le=90)
    # Probation length in months. Capped at 2 years — anything longer is a data
    # entry mistake, and it would keep someone on the lowest band indefinitely.
    probation_months: int | None = Field(default=None, ge=0, le=24)

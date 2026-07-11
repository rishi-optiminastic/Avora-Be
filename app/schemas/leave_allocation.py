"""Leave-allocation request/response schemas (Golden rule #5).

A null `planned_days`/`sick_days` means "use the org policy default" — the
write schema accepts null explicitly so HR/Admin can clear an override, not
just set one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class LeaveAllocationWrite(BaseModel):
    """HR/Admin set (or clear, with null) an employee's leave-quota override."""

    planned_days: int | None = Field(default=None, ge=0, le=365)
    sick_days: int | None = Field(default=None, ge=0, le=365)
    annual_days: int | None = Field(default=None, ge=0, le=365)
    bereavement_days: int | None = Field(default=None, ge=0, le=365)
    birthday_days: int | None = Field(default=None, ge=0, le=365)
    maternity_days: int | None = Field(default=None, ge=0, le=365)
    paternity_days: int | None = Field(default=None, ge=0, le=365)
    marriage_days: int | None = Field(default=None, ge=0, le=365)
    note: str | None = Field(default=None, max_length=500)


class LeaveAllocationRead(ORMModel):
    employee_id: uuid.UUID
    planned_days: int | None
    sick_days: int | None
    annual_days: int | None
    bereavement_days: int | None
    birthday_days: int | None
    maternity_days: int | None
    paternity_days: int | None
    marriage_days: int | None
    note: str | None
    updated_by: uuid.UUID | None
    updated_at: datetime

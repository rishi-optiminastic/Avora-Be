"""Leave-allocation request/response schemas (Golden rule #5).

A null `planned_days`/`sick_days` means "use the org policy default" — the
write schema accepts null explicitly so HR/Admin can clear an override, not
just set one.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

# The "taken before Avora" fields, in one place so the repository, the service
# and the tests agree on what counts as "HR entered some".
USED_BEFORE_FIELDS: tuple[str, ...] = (
    "planned_used_before",
    "sick_used_before",
    "annual_used_before",
    "bereavement_used_before",
    "birthday_used_before",
    "maternity_used_before",
    "paternity_used_before",
    "marriage_used_before",
)

# Half days are the finest grain leave is booked in, so they are the finest
# grain HR may enter here; a quarter day could never be reconciled.
_HALF_DAY = 0.5


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

    # Days of each type already taken this leave year before Avora held leave
    # records. Counted as USED (the quota is untouched). Null = none.
    planned_used_before: float | None = Field(default=None, ge=0, le=366, multiple_of=_HALF_DAY)
    sick_used_before: float | None = Field(default=None, ge=0, le=366, multiple_of=_HALF_DAY)
    annual_used_before: float | None = Field(default=None, ge=0, le=366, multiple_of=_HALF_DAY)
    bereavement_used_before: float | None = Field(default=None, ge=0, le=366, multiple_of=_HALF_DAY)
    birthday_used_before: float | None = Field(default=None, ge=0, le=366, multiple_of=_HALF_DAY)
    maternity_used_before: float | None = Field(default=None, ge=0, le=366, multiple_of=_HALF_DAY)
    paternity_used_before: float | None = Field(default=None, ge=0, le=366, multiple_of=_HALF_DAY)
    marriage_used_before: float | None = Field(default=None, ge=0, le=366, multiple_of=_HALF_DAY)
    note: str | None = Field(default=None, max_length=500)

    def used_before_entered(self) -> bool:
        """Whether HR entered any taken-before-Avora days at all."""
        return any(getattr(self, field) is not None for field in USED_BEFORE_FIELDS)


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
    planned_used_before: float | None
    sick_used_before: float | None
    annual_used_before: float | None
    bereavement_used_before: float | None
    birthday_used_before: float | None
    maternity_used_before: float | None
    paternity_used_before: float | None
    marriage_used_before: float | None
    # The leave year (its 1 April) the taken-before figures count in.
    used_before_year_start: date | None
    note: str | None
    updated_by: uuid.UUID | None
    updated_at: datetime

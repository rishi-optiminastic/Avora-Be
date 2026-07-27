"""Attendance override endpoints — HR/Admin force a day's status.

List/set/remove per-employee day overrides (full-day / half-day / absent) that the
attendance computation applies, feeding the payroll counts. HR/Admin only; enforced
in the service. Applied directly (no approval), audited.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import AttendanceOverrideServiceDep, CurrentUserDep
from app.schemas.attendance_override import AttendanceOverrideCreate, AttendanceOverrideRead

router = APIRouter(prefix="/attendance/overrides", tags=["attendance"])


@router.get("", response_model=list[AttendanceOverrideRead])
async def list_attendance_overrides(
    caller: CurrentUserDep,
    service: AttendanceOverrideServiceDep,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> list[AttendanceOverrideRead]:
    rows = await service.list_for_period(caller, month)
    return [AttendanceOverrideRead.model_validate(r) for r in rows]


@router.post("", response_model=AttendanceOverrideRead, status_code=status.HTTP_201_CREATED)
async def set_attendance_override(
    payload: AttendanceOverrideCreate,
    caller: CurrentUserDep,
    service: AttendanceOverrideServiceDep,
) -> AttendanceOverrideRead:
    """Set (or replace) the override for one employee on one day."""
    return AttendanceOverrideRead.model_validate(await service.upsert(caller, payload))


@router.delete("/{override_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attendance_override(
    override_id: uuid.UUID,
    caller: CurrentUserDep,
    service: AttendanceOverrideServiceDep,
) -> None:
    await service.delete(caller, override_id)

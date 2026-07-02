"""Leave-allocation endpoints — HR/Admin only (per-employee quota override).

Authorization lives in `LeaveAllocationService`: HR/Admin read & write anyone's;
everyone else gets 403. An employee's *effective* quota (override or org
default) is visible to them separately, via `GET /leaves/balance`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.core.deps import CurrentUserDep, LeaveAllocationServiceDep
from app.schemas.leave_allocation import LeaveAllocationRead, LeaveAllocationWrite

router = APIRouter(prefix="/employees", tags=["leave-allocation"])


@router.get("/{employee_id}/leave-allocation", response_model=LeaveAllocationRead)
async def get_leave_allocation(
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: LeaveAllocationServiceDep,
) -> LeaveAllocationRead:
    return LeaveAllocationRead.model_validate(await service.get(caller, employee_id))


@router.put("/{employee_id}/leave-allocation", response_model=LeaveAllocationRead)
async def set_leave_allocation(
    employee_id: uuid.UUID,
    payload: LeaveAllocationWrite,
    caller: CurrentUserDep,
    service: LeaveAllocationServiceDep,
) -> LeaveAllocationRead:
    return LeaveAllocationRead.model_validate(await service.set(caller, employee_id, payload))

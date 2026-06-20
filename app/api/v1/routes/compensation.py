"""Compensation endpoints — HR/Admin manage; the person may read their own.

Authorization lives in `CompensationService`: HR/Admin read & write anyone's;
everyone else may read only their own and may never write. An out-of-scope read
returns 403 (the employee's existence isn't secret — only their pay is).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.core.deps import CompensationServiceDep, CurrentUserDep
from app.schemas.compensation import CompensationRead, CompensationWrite

router = APIRouter(prefix="/employees", tags=["compensation"])


@router.get("/{employee_id}/compensation", response_model=CompensationRead)
async def get_compensation(
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: CompensationServiceDep,
) -> CompensationRead:
    return CompensationRead.model_validate(await service.get(caller, employee_id))


@router.put("/{employee_id}/compensation", response_model=CompensationRead)
async def set_compensation(
    employee_id: uuid.UUID,
    payload: CompensationWrite,
    caller: CurrentUserDep,
    service: CompensationServiceDep,
) -> CompensationRead:
    return CompensationRead.model_validate(await service.set(caller, employee_id, payload))

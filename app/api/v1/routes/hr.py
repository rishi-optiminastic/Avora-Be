"""HR sync webhook.

HMAC + IP allowlist are enforced by `verify_hr_webhook` before the handler.
The handler depends on that verifier (as a side-effecting guard) and only then
processes the strictly-typed payload, which structurally cannot carry privilege.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.deps import HRServiceDep, verify_hr_webhook
from app.schemas.employee import EmployeeRead, HREmployeeUpsert

router = APIRouter(prefix="/hr", tags=["hr"])


@router.post("/sync", response_model=EmployeeRead, status_code=status.HTTP_200_OK)
async def hr_sync(
    payload: HREmployeeUpsert,
    service: HRServiceDep,
    _verified: Annotated[bytes, Depends(verify_hr_webhook)],
) -> EmployeeRead:
    employee = await service.sync_employee(payload)
    return EmployeeRead.model_validate(employee)

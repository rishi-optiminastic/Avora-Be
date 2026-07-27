"""Payroll adjustment endpoints — HR/Admin manual tweaks to a month.

List/add/remove per-employee earnings, deductions, and field overrides that the
payroll estimate + register apply on top of the formula. HR/Admin only; enforced
in the service. Applied directly (no approval), audited.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUserDep, PayrollAdjustmentServiceDep
from app.schemas.payroll_adjustment import PayrollAdjustmentCreate, PayrollAdjustmentRead

router = APIRouter(prefix="/payroll/adjustments", tags=["payroll"])


@router.get("", response_model=list[PayrollAdjustmentRead])
async def list_payroll_adjustments(
    caller: CurrentUserDep,
    service: PayrollAdjustmentServiceDep,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> list[PayrollAdjustmentRead]:
    rows = await service.list_for_period(caller, month)
    return [PayrollAdjustmentRead.model_validate(r) for r in rows]


@router.post("", response_model=PayrollAdjustmentRead, status_code=status.HTTP_201_CREATED)
async def create_payroll_adjustment(
    payload: PayrollAdjustmentCreate,
    caller: CurrentUserDep,
    service: PayrollAdjustmentServiceDep,
) -> PayrollAdjustmentRead:
    return PayrollAdjustmentRead.model_validate(await service.create(caller, payload))


@router.delete("/{adjustment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payroll_adjustment(
    adjustment_id: uuid.UUID,
    caller: CurrentUserDep,
    service: PayrollAdjustmentServiceDep,
) -> None:
    await service.delete(caller, adjustment_id)

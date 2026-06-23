"""Payroll endpoints.

Most are HR/Admin only (org-wide salary, settings, HR digest); authorization
lives in `PayrollService`, so an out-of-scope caller gets 403. The one
employee-facing endpoint is `GET /payroll/me`: a person's own slip, self-or-HR
scoped. Money is returned in minor units; the client formats it.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.deps import CurrentUserDep, PayrollServiceDep
from app.schemas.payroll import (
    PayrollEstimateRead,
    PayrollRunRead,
    PayrollSettingsRead,
    PayrollSettingsUpdate,
    PayslipRead,
)

router = APIRouter(prefix="/payroll", tags=["payroll"])


@router.get("/me", response_model=PayslipRead)
async def get_my_payslip(
    caller: CurrentUserDep,
    service: PayrollServiceDep,
    month: str | None = Query(default=None, description="YYYY-MM; defaults to the current month"),
) -> PayslipRead:
    """The caller's own salary slip for a month (self-service 'My Pay')."""
    return await service.my_slip(caller, None, month)


@router.get("/settings", response_model=PayrollSettingsRead)
async def get_payroll_settings(
    caller: CurrentUserDep, service: PayrollServiceDep
) -> PayrollSettingsRead:
    return await service.get_settings(caller)


@router.put("/settings", response_model=PayrollSettingsRead)
async def update_payroll_settings(
    payload: PayrollSettingsUpdate, caller: CurrentUserDep, service: PayrollServiceDep
) -> PayrollSettingsRead:
    return await service.update_settings(caller, payload)


@router.get("/estimate", response_model=PayrollEstimateRead)
async def get_payroll_estimate(
    caller: CurrentUserDep,
    service: PayrollServiceDep,
    month: str | None = Query(default=None, description="YYYY-MM; defaults to the current month"),
) -> PayrollEstimateRead:
    return await service.estimate(caller, month)


@router.post("/send", response_model=PayrollRunRead)
async def send_payroll_digest(
    caller: CurrentUserDep,
    service: PayrollServiceDep,
    month: str | None = Query(default=None, description="YYYY-MM; defaults to the current month"),
) -> PayrollRunRead:
    return await service.send_digest(caller, month)


@router.get("/runs", response_model=list[PayrollRunRead])
async def list_payroll_runs(
    caller: CurrentUserDep, service: PayrollServiceDep
) -> list[PayrollRunRead]:
    return await service.list_runs(caller)

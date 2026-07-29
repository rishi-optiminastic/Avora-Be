"""Payroll endpoints.

Most are HR/Admin only (org-wide salary, settings, HR digest); authorization
lives in `PayrollService`, so an out-of-scope caller gets 403. The one
employee-facing endpoint is `GET /payroll/me`: a person's own slip, self-or-HR
scoped. Money is returned in minor units; the client formats it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.core.deps import CurrentUserDep, PayrollServiceDep
from app.schemas.payroll import (
    PayrollEstimateRead,
    PayrollFinalizeResult,
    PayrollRunRead,
    PayrollSettingsRead,
    PayrollSettingsUpdate,
    PayslipRead,
    PayslipSummaryRead,
    ReleasedPayslipRead,
)

router = APIRouter(prefix="/payroll", tags=["payroll"])


@router.get("/me", response_model=PayslipRead)
async def get_my_payslip(
    caller: CurrentUserDep,
    service: PayrollServiceDep,
    month: str | None = Query(default=None, description="YYYY-MM; defaults to the current month"),
    employee_id: Annotated[
        uuid.UUID | None,
        Query(description="Whose live slip (self-or-HR/Admin); defaults to the caller"),
    ] = None,
) -> PayslipRead:
    """A person's own LIVE slip for a month (recomputed; an HR preview before a
    month is finalized). Self-or-HR: an employee sees only their own, HR/Admin may
    pass any `employee_id`. Released history lives under `/payroll/payslips`."""
    return await service.my_slip(caller, employee_id, month)


@router.get("/payslips", response_model=list[PayslipSummaryRead])
async def list_payslips(
    caller: CurrentUserDep,
    service: PayrollServiceDep,
    employee_id: Annotated[
        uuid.UUID | None,
        Query(description="Whose payslips (HR/Admin only); defaults to the caller"),
    ] = None,
) -> list[PayslipSummaryRead]:
    """An employee's released-payslip history — self, or anyone's for HR/Admin."""
    return await service.list_payslips(caller, employee_id)


@router.get("/payslips/{month}", response_model=ReleasedPayslipRead)
async def get_released_payslip(
    month: str,
    caller: CurrentUserDep,
    service: PayrollServiceDep,
    employee_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ReleasedPayslipRead:
    """One released payslip (404 until HR finalizes the month). Self-or-HR."""
    return await service.get_released_payslip(caller, employee_id, month)


@router.get("/me/pdf")
async def generate_payslip_pdf(
    caller: CurrentUserDep,
    service: PayrollServiceDep,
    month: str | None = Query(default=None, description="YYYY-MM; defaults to the current month"),
    employee_id: Annotated[
        uuid.UUID | None,
        Query(description="Whose payslip (self-or-HR/Admin); defaults to the caller"),
    ] = None,
) -> Response:
    """Generate + download a payslip PDF from the LIVE slip (no finalize needed).
    Self-or-HR/Admin: an employee generates their own; HR/Admin generate anyone's."""
    pdf, filename = await service.live_payslip_pdf(caller, employee_id, month)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/payslips/{month}/pdf")
async def download_payslip_pdf(
    month: str,
    caller: CurrentUserDep,
    service: PayrollServiceDep,
    employee_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Response:
    """Download a released payslip as a PDF (404 until finalized). Self-or-HR."""
    pdf, filename = await service.payslip_pdf(caller, employee_id, month)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/export")
async def export_payroll_xlsx(
    caller: CurrentUserDep,
    service: PayrollServiceDep,
    month: str | None = Query(default=None, description="YYYY-MM; defaults to the current month"),
) -> Response:
    """HR/Admin: download the month's payroll as an .xlsx (identity + bank details
    + UAN + the LOP-adjusted salary breakdown, one row per employee)."""
    xlsx, filename = await service.export_xlsx(caller, month)
    return Response(
        content=xlsx,
        media_type=_XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/finalize", response_model=PayrollFinalizeResult)
async def finalize_payroll(
    caller: CurrentUserDep,
    service: PayrollServiceDep,
    month: str | None = Query(default=None, description="YYYY-MM; defaults to the current month"),
) -> PayrollFinalizeResult:
    """HR/Admin: freeze + release the month's payslips and email everyone their PDF."""
    return await service.finalize(caller, month)


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

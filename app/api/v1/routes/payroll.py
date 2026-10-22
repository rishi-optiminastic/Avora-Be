"""Payroll endpoints.

Most are HR/Admin only (org-wide salary, settings, HR digest); authorization
lives in `PayrollService`, so an out-of-scope caller gets 403. The one
employee-facing endpoint is `GET /payroll/me`: a person's own slip, self-or-HR
scoped. Money is returned in minor units; the client formats it.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError as PydanticValidationError

from app.core.deps import CurrentUserDep, PayrollReceiptServiceDep, PayrollServiceDep
from app.core.exceptions import ValidationError
from app.models.payroll_receipt import PayrollReceiptKind
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
from app.schemas.payroll_receipt import (
    PayrollProofStatus,
    PayrollReceiptCreate,
    PayrollReceiptRead,
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
    employee_ids: Annotated[
        list[uuid.UUID] | None,
        Query(description="Restrict the register to these employees; omit for everyone"),
    ] = None,
    include_reimbursements: Annotated[
        bool,
        Query(
            description=(
                "Fold approved reimbursements into Net Pay. Set false when expenses "
                "are paid from their own run via /export/reimbursements."
            )
        ),
    ] = True,
) -> Response:
    """HR/Admin: download the month's payroll as an .xlsx (identity + bank details
    + UAN + the LOP-adjusted salary breakdown, one row per employee). Pass
    `employee_ids` to export only a selection — it narrows the rows the caller can
    already see, so it never widens scope."""
    xlsx, filename = await service.export_xlsx(
        caller, month, employee_ids, include_reimbursements=include_reimbursements
    )
    return Response(
        content=xlsx,
        media_type=_XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/reimbursements")
async def export_reimbursements_xlsx(
    caller: CurrentUserDep,
    service: PayrollServiceDep,
    month: str | None = Query(default=None, description="YYYY-MM; defaults to the current month"),
) -> Response:
    """HR/Admin: the month's approved reimbursements as their own .xlsx.

    Same 40-column layout as the salary register so it imports the same way, but
    salary-free: only people with an approved claim, and the only money on the row
    is what they are owed back. 404 when nothing is approved for the month.

    Pair it with `include_reimbursements=false` on /export, or the same rupees go
    out in both files.
    """
    xlsx, filename = await service.export_reimbursements_xlsx(caller, month)
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


# --------------------------------------------------------------------------- #
# Proof of payment
#
# What payroll SAID should go out lives in the run and the register. These are
# the documents showing it actually did - the bank advice, the UTR
# confirmation - filed by whoever ran it. Payroll-manager gated like the rest
# of the cluster: a transfer advice carries the payroll total and the company's
# own bank references.
# --------------------------------------------------------------------------- #
@router.post("/receipts", response_model=PayrollReceiptRead, status_code=status.HTTP_201_CREATED)
async def add_payroll_receipt(
    caller: CurrentUserDep,
    service: PayrollReceiptServiceDep,
    period_month: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    kind: Annotated[PayrollReceiptKind, Form()] = PayrollReceiptKind.BANK_ADVICE,
    reference: Annotated[str | None, Form()] = None,
    paid_on: Annotated[date | None, Form()] = None,
    amount_minor: Annotated[int | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> PayrollReceiptRead:
    """File proof that a month's payroll was paid (PDF or image, max 15 MB).

    Multipart because the document rides along. Several per month is normal:
    salaries and reimbursements often go out as separate transfers.
    """
    try:
        meta = PayrollReceiptCreate(
            period_month=period_month,
            kind=kind,
            reference=reference,
            paid_on=paid_on,
            amount_minor=amount_minor,
            note=note,
        )
    except PydanticValidationError as exc:
        # Form fields are validated here rather than by FastAPI, so hand the
        # errors to the same 422 handler a JSON body would reach instead of
        # letting a raw Pydantic error escape as a 500.
        raise RequestValidationError(
            [{**err, "loc": ("body", *err["loc"])} for err in exc.errors(include_url=False)]
        ) from exc
    data = await file.read()
    if not file.content_type:
        raise ValidationError("The upload is missing its content type.")
    return await service.add(
        caller,
        meta,
        filename=file.filename or "receipt",
        content_type=file.content_type,
        data=data,
    )


@router.get("/receipts", response_model=list[PayrollReceiptRead])
async def list_payroll_receipts(
    caller: CurrentUserDep,
    service: PayrollReceiptServiceDep,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> list[PayrollReceiptRead]:
    return await service.list_for_month(caller, month)


@router.get("/receipts/status", response_model=PayrollProofStatus)
async def payroll_proof_status(
    caller: CurrentUserDep,
    service: PayrollReceiptServiceDep,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> PayrollProofStatus:
    """Whether this month can be shown to have been paid. Read by the payroll
    panel so an unevidenced month is visible rather than merely absent."""
    return await service.proof_status(caller, month)


@router.get("/receipts/{receipt_id}/file")
async def download_payroll_receipt(
    receipt_id: uuid.UUID, caller: CurrentUserDep, service: PayrollReceiptServiceDep
) -> Response:
    data, content_type, filename = await service.file_bytes(caller, receipt_id)
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/receipts/{receipt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_payroll_receipt(
    receipt_id: uuid.UUID, caller: CurrentUserDep, service: PayrollReceiptServiceDep
) -> None:
    """Filed the wrong document, or against the wrong month. The removal is
    itself audited - the trail is append-only."""
    await service.remove(caller, receipt_id)

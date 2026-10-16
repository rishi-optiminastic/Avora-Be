"""Payout verification endpoints - the bank payout file checked against payroll.

Payroll managers only (HR/Admin or the payroll-manager grant); authorization
lives in `PayoutVerificationService`, so anyone else gets 403 like the rest of
the payroll cluster. Money is returned in minor units; the client formats it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile, status

from app.core.deps import CurrentUserDep, PayoutVerificationServiceDep, UploadRateLimitDep
from app.core.exceptions import ValidationError
from app.core.payout_file import MAX_PAYOUT_FILE_BYTES
from app.models.payout_verification import PayoutRunKind
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.payout_verification import (
    PayoutMismatchRead,
    PayoutMismatchResolve,
    PayoutUploadRead,
    PayoutVerificationRead,
)

router = APIRouter(prefix="/payroll/payouts", tags=["payroll"])

_MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
MonthQuery = Annotated[str, Query(pattern=_MONTH_PATTERN, description="YYYY-MM")]
OptionalMonthQuery = Annotated[str | None, Query(pattern=_MONTH_PATTERN, description="YYYY-MM")]


@router.post("", response_model=PayoutVerificationRead, status_code=status.HTTP_201_CREATED)
async def upload_payout(
    caller: UploadRateLimitDep,
    service: PayoutVerificationServiceDep,
    month: MonthQuery,
    file: Annotated[UploadFile, File()],
    kind: Annotated[
        PayoutRunKind,
        Query(description="Which payroll export the file was paid from"),
    ] = PayoutRunKind.SALARY,
) -> PayoutVerificationRead:
    """Upload the bank payout file (.xlsx or .csv, 5 MB) for a payroll month.

    The file is verified on upload and the result returned; when anything does
    not match, HR and payroll managers are notified once. An unusable file is a
    422 naming the row; the same file again for the same month and run is a 409.
    """
    data = await file.read(MAX_PAYOUT_FILE_BYTES + 1)
    if len(data) > MAX_PAYOUT_FILE_BYTES:
        raise ValidationError("The file is larger than 5 MB.")
    return await service.upload(caller, month=month, kind=kind, data=data, filename=file.filename)


@router.get("", response_model=Page[PayoutUploadRead])
async def list_payouts(
    caller: CurrentUserDep,
    service: PayoutVerificationServiceDep,
    month: OptionalMonthQuery = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> Page[PayoutUploadRead]:
    """Past payout uploads, newest first, optionally for one month."""
    items, total = await service.list_uploads(caller, month, limit=size, offset=(page - 1) * size)
    return Page[PayoutUploadRead](items=items, page=page, size=size, total=total)


@router.get("/latest", response_model=PayoutVerificationRead)
async def latest_payout(
    caller: CurrentUserDep,
    service: PayoutVerificationServiceDep,
    month: OptionalMonthQuery = None,
) -> PayoutVerificationRead:
    """The most recent verification (for a month, or overall). 404 when none."""
    return await service.latest(caller, month)


@router.get("/{upload_id}", response_model=PayoutVerificationRead)
async def get_payout(
    upload_id: uuid.UUID, caller: CurrentUserDep, service: PayoutVerificationServiceDep
) -> PayoutVerificationRead:
    return await service.get(caller, upload_id)


@router.post("/{upload_id}/mismatches/{mismatch_id}/resolve", response_model=PayoutMismatchRead)
async def resolve_mismatch(
    upload_id: uuid.UUID,
    mismatch_id: uuid.UUID,
    payload: PayoutMismatchResolve,
    caller: CurrentUserDep,
    service: PayoutVerificationServiceDep,
) -> PayoutMismatchRead:
    """Mark one mismatch resolved with a note on what was done. Audited."""
    return await service.resolve(caller, upload_id, mismatch_id, payload.note)

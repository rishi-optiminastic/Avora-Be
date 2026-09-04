"""Reimbursement endpoints — employee submits/withdraws; manager then HR decide.

Two-step approval: the reporting manager acts via `/manager-decision`, then
HR/Admin finalise via `/hr-decision`. Reads are scoped to the caller in the
repository (own + reports'/dept + HR/Admin all). Authorization lives in the
service (Golden rule #3).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from fastapi.responses import RedirectResponse

from app.core import storage
from app.core.deps import (
    CurrentUserDep,
    DownloadRateLimitDep,
    ReimbursementServiceDep,
    UploadRateLimitDep,
)
from app.core.exceptions import NotFoundError
from app.models.reimbursement import ReimbursementStatus
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.reimbursement import (
    ReimbursementCreate,
    ReimbursementDecision,
    ReimbursementRead,
)

router = APIRouter(prefix="/reimbursements", tags=["reimbursements"])


def _safe_filename(name: str) -> str:
    """Strip path separators and quotes so a filename can't break the header."""
    cleaned = name.replace("\\", "/").split("/")[-1].replace('"', "").strip()
    return cleaned or "invoice"


@router.get("", response_model=Page[ReimbursementRead])
async def list_reimbursements(
    caller: CurrentUserDep,
    service: ReimbursementServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    status_filter: Annotated[ReimbursementStatus | None, Query(alias="status")] = None,
) -> Page[ReimbursementRead]:
    rows, total = await service.list_for_caller(
        caller, offset=(page - 1) * size, limit=size, status=status_filter
    )
    return Page(
        items=[ReimbursementRead.model_validate(r) for r in rows],
        page=page,
        size=size,
        total=total,
    )


@router.post("", response_model=ReimbursementRead, status_code=status.HTTP_201_CREATED)
async def submit_reimbursement(
    payload: ReimbursementCreate,
    caller: CurrentUserDep,
    service: ReimbursementServiceDep,
) -> ReimbursementRead:
    return ReimbursementRead.model_validate(await service.submit(caller, payload))


@router.get("/{reimbursement_id}", response_model=ReimbursementRead)
async def get_reimbursement(
    reimbursement_id: uuid.UUID,
    caller: CurrentUserDep,
    service: ReimbursementServiceDep,
) -> ReimbursementRead:
    return ReimbursementRead.model_validate(
        await service.get_for_caller(caller, reimbursement_id)
    )


@router.post("/{reimbursement_id}/manager-decision", response_model=ReimbursementRead)
async def manager_decide_reimbursement(
    reimbursement_id: uuid.UUID,
    payload: ReimbursementDecision,
    caller: CurrentUserDep,
    service: ReimbursementServiceDep,
) -> ReimbursementRead:
    """Step 1 — the applicant's reporting manager (or HR/Admin) approves/rejects."""
    return ReimbursementRead.model_validate(
        await service.manager_decide(caller, reimbursement_id, payload)
    )


@router.post("/{reimbursement_id}/hr-decision", response_model=ReimbursementRead)
async def hr_decide_reimbursement(
    reimbursement_id: uuid.UUID,
    payload: ReimbursementDecision,
    caller: CurrentUserDep,
    service: ReimbursementServiceDep,
) -> ReimbursementRead:
    """Step 2 — HR/Admin final approval of a manager-approved claim."""
    return ReimbursementRead.model_validate(
        await service.hr_decide(caller, reimbursement_id, payload)
    )


@router.post("/{reimbursement_id}/withdraw", response_model=ReimbursementRead)
async def withdraw_reimbursement(
    reimbursement_id: uuid.UUID,
    caller: CurrentUserDep,
    service: ReimbursementServiceDep,
) -> ReimbursementRead:
    return ReimbursementRead.model_validate(
        await service.withdraw(caller, reimbursement_id)
    )


@router.post(
    "/{reimbursement_id}/receipts",
    response_model=ReimbursementRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_receipt(
    reimbursement_id: uuid.UUID,
    caller: UploadRateLimitDep,
    service: ReimbursementServiceDep,
    file: Annotated[UploadFile, File()],
    label: Annotated[str | None, Form()] = None,
) -> ReimbursementRead:
    """Attach one named proof to your own claim, while it can still be edited.

    PDF or image, 10 MB each, up to ten per claim. `label` is what the reviewer
    sees in the list; it falls back to the filename when omitted.
    """
    data = await file.read()
    row = await service.add_receipt(
        caller,
        reimbursement_id,
        data,
        label=label,
        filename=file.filename,
        content_type=file.content_type or "application/octet-stream",
    )
    return ReimbursementRead.model_validate(row)


@router.delete("/{reimbursement_id}/receipts/{receipt_id}", response_model=ReimbursementRead)
async def remove_receipt(
    reimbursement_id: uuid.UUID,
    receipt_id: uuid.UUID,
    caller: CurrentUserDep,
    service: ReimbursementServiceDep,
) -> ReimbursementRead:
    """Remove one proof from your own claim, while it can still be edited."""
    row = await service.remove_receipt(caller, reimbursement_id, receipt_id)
    return ReimbursementRead.model_validate(row)


@router.get("/{reimbursement_id}/receipts/{receipt_id}")
async def download_receipt(
    reimbursement_id: uuid.UUID,
    receipt_id: uuid.UUID,
    caller: DownloadRateLimitDep,
    service: ReimbursementServiceDep,
) -> Response:
    """One proof, scoped to whoever may see the claim (404 otherwise).

    Served as a neutral-type attachment — an uploaded file is never handed back
    with a type a browser will execute.
    """
    receipt = await service.get_receipt(caller, reimbursement_id, receipt_id)
    safe = _safe_filename(receipt.filename or receipt.label or "proof")
    if receipt.object_key:
        url = storage.presigned_get_url(receipt.object_key, download_filename=safe)
        return RedirectResponse(url, status_code=307)
    if receipt.content is not None:
        return Response(
            content=receipt.content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )
    raise NotFoundError()

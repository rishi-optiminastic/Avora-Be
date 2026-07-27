"""Reimbursement endpoints — employee submits/withdraws; manager then HR decide.

Two-step approval: the reporting manager acts via `/manager-decision`, then
HR/Admin finalise via `/hr-decision`. Reads are scoped to the caller in the
repository (own + reports'/dept + HR/Admin all). Authorization lives in the
service (Golden rule #3).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUserDep, ReimbursementServiceDep
from app.models.reimbursement import ReimbursementStatus
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.reimbursement import (
    ReimbursementCreate,
    ReimbursementDecision,
    ReimbursementRead,
)

router = APIRouter(prefix="/reimbursements", tags=["reimbursements"])


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

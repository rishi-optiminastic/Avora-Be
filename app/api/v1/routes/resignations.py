"""Resignation endpoints — employee submits/withdraws; HR/Admin decide.

Reads are scoped to the caller (own + HR/Admin all). Authorization lives in the
service; every read is scoped in the repository (Golden rule #3).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUserDep, ResignationServiceDep
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.resignation import (
    ResignationCreate,
    ResignationDecision,
    ResignationRead,
)

router = APIRouter(prefix="/resignations", tags=["resignations"])


@router.get("", response_model=Page[ResignationRead])
async def list_resignations(
    caller: CurrentUserDep,
    service: ResignationServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> Page[ResignationRead]:
    rows, total = await service.list_for_caller(caller, offset=(page - 1) * size, limit=size)
    return Page(
        items=[ResignationRead.model_validate(r) for r in rows],
        page=page,
        size=size,
        total=total,
    )


@router.post("", response_model=ResignationRead, status_code=status.HTTP_201_CREATED)
async def submit_resignation(
    payload: ResignationCreate,
    caller: CurrentUserDep,
    service: ResignationServiceDep,
) -> ResignationRead:
    return ResignationRead.model_validate(await service.submit(caller, payload))


@router.get("/{resignation_id}", response_model=ResignationRead)
async def get_resignation(
    resignation_id: uuid.UUID,
    caller: CurrentUserDep,
    service: ResignationServiceDep,
) -> ResignationRead:
    return ResignationRead.model_validate(await service.get_for_caller(caller, resignation_id))


@router.post("/{resignation_id}/decision", response_model=ResignationRead)
async def decide_resignation(
    resignation_id: uuid.UUID,
    payload: ResignationDecision,
    caller: CurrentUserDep,
    service: ResignationServiceDep,
) -> ResignationRead:
    """Accept or reject a submitted resignation (HR/Admin, never your own)."""
    return ResignationRead.model_validate(
        await service.decide(caller, resignation_id, payload)
    )


@router.post("/{resignation_id}/withdraw", response_model=ResignationRead)
async def withdraw_resignation(
    resignation_id: uuid.UUID,
    caller: CurrentUserDep,
    service: ResignationServiceDep,
) -> ResignationRead:
    return ResignationRead.model_validate(await service.withdraw(caller, resignation_id))

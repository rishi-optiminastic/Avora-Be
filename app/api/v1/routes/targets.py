"""Target endpoints — assign (admin/senior manager) + track (owner), all scoped."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUserDep, TargetServiceDep
from app.models.target import TargetPeriod, TargetStatus
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.target import TargetCreate, TargetRead, TargetUpdate

router = APIRouter(prefix="/targets", tags=["targets"])


@router.get("", response_model=Page[TargetRead])
async def list_targets(
    caller: CurrentUserDep,
    service: TargetServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    period: TargetPeriod | None = None,
    target_status: Annotated[TargetStatus | None, Query(alias="status")] = None,
) -> Page[TargetRead]:
    offset = (page - 1) * size
    items, total = await service.list_for_caller(
        caller, offset=offset, limit=size, period=period, status=target_status
    )
    return Page[TargetRead](
        items=[TargetRead.model_validate(t) for t in items], page=page, size=size, total=total
    )


@router.post("", response_model=TargetRead, status_code=status.HTTP_201_CREATED)
async def create_target(
    payload: TargetCreate,
    caller: CurrentUserDep,
    service: TargetServiceDep,
) -> TargetRead:
    return TargetRead.model_validate(await service.create(caller, payload))


@router.get("/{target_id}", response_model=TargetRead)
async def get_target(
    target_id: uuid.UUID,
    caller: CurrentUserDep,
    service: TargetServiceDep,
) -> TargetRead:
    return TargetRead.model_validate(await service.get_for_caller(caller, target_id))


@router.patch("/{target_id}", response_model=TargetRead)
async def update_target(
    target_id: uuid.UUID,
    payload: TargetUpdate,
    caller: CurrentUserDep,
    service: TargetServiceDep,
) -> TargetRead:
    return TargetRead.model_validate(await service.update(caller, target_id, payload))


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    target_id: uuid.UUID,
    caller: CurrentUserDep,
    service: TargetServiceDep,
) -> None:
    await service.delete(caller, target_id)

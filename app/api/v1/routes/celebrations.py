"""Celebration endpoints — HR/Admin manage greeting toggles + festivals.

The daily broadcast itself runs in the celebration scheduler; these endpoints are
the admin surface for the on/off switches and the festival list.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import CelebrationServiceDep, CurrentUserDep
from app.schemas.celebration import (
    CelebrationSettingsRead,
    CelebrationSettingsUpdate,
    FestivalCreate,
    FestivalRead,
)

router = APIRouter(prefix="/celebrations", tags=["celebrations"])


@router.get("/settings", response_model=CelebrationSettingsRead)
async def get_settings(
    caller: CurrentUserDep, service: CelebrationServiceDep
) -> CelebrationSettingsRead:
    """The greeting toggles — readable by everyone; only HR/Admin can change them."""
    return CelebrationSettingsRead.from_model(await service.get_or_create_settings())


@router.put("/settings", response_model=CelebrationSettingsRead)
async def update_settings(
    payload: CelebrationSettingsUpdate,
    caller: CurrentUserDep,
    service: CelebrationServiceDep,
) -> CelebrationSettingsRead:
    return CelebrationSettingsRead.from_model(await service.update_settings(caller, payload))


@router.get("/festivals", response_model=list[FestivalRead])
async def list_festivals(
    caller: CurrentUserDep, service: CelebrationServiceDep
) -> list[FestivalRead]:
    festivals = await service.list_festivals(caller)
    return [FestivalRead.model_validate(f) for f in festivals]


@router.post("/festivals", response_model=FestivalRead, status_code=status.HTTP_201_CREATED)
async def add_festival(
    payload: FestivalCreate,
    caller: CurrentUserDep,
    service: CelebrationServiceDep,
) -> FestivalRead:
    return FestivalRead.model_validate(await service.add_festival(caller, payload))


@router.delete("/festivals/{festival_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_festival(
    festival_id: uuid.UUID,
    caller: CurrentUserDep,
    service: CelebrationServiceDep,
) -> None:
    await service.delete_festival(caller, festival_id)

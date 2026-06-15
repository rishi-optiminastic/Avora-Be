"""Holiday endpoints — readable by everyone, mutable only by HR/admin."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUserDep, HolidayServiceDep
from app.schemas.common import MAX_PAGE_SIZE, Page
from app.schemas.holiday import HolidayCreate, HolidayRead, HolidayUpdate

router = APIRouter(prefix="/holidays", tags=["holidays"])


@router.get("", response_model=Page[HolidayRead])
async def list_holidays(
    caller: CurrentUserDep,
    service: HolidayServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = MAX_PAGE_SIZE,
) -> Page[HolidayRead]:
    offset = (page - 1) * size
    items, total = await service.list_all(offset=offset, limit=size)
    return Page[HolidayRead](
        items=[HolidayRead.model_validate(h) for h in items],
        page=page,
        size=size,
        total=total,
    )


@router.post("", response_model=HolidayRead, status_code=status.HTTP_201_CREATED)
async def create_holiday(
    payload: HolidayCreate,
    caller: CurrentUserDep,
    service: HolidayServiceDep,
) -> HolidayRead:
    return HolidayRead.model_validate(await service.create(caller, payload))


@router.patch("/{holiday_id}", response_model=HolidayRead)
async def update_holiday(
    holiday_id: uuid.UUID,
    payload: HolidayUpdate,
    caller: CurrentUserDep,
    service: HolidayServiceDep,
) -> HolidayRead:
    return HolidayRead.model_validate(await service.update(caller, holiday_id, payload))


@router.delete("/{holiday_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_holiday(
    holiday_id: uuid.UUID,
    caller: CurrentUserDep,
    service: HolidayServiceDep,
) -> None:
    await service.delete(caller, holiday_id)

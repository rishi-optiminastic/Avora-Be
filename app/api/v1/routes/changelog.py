"""Changelog endpoints.

`GET` is open to every authenticated employee (the "What's new" page). `POST`,
`PATCH`, and `DELETE` are admin-only — authorization lives in ChangelogService,
so an out-of-scope caller gets 403.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import ChangelogServiceDep, CurrentUserDep
from app.schemas.changelog import ChangelogCreate, ChangelogRead, ChangelogUpdate
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page

router = APIRouter(prefix="/changelog", tags=["changelog"])


@router.get("", response_model=Page[ChangelogRead])
async def list_changelog(
    caller: CurrentUserDep,
    service: ChangelogServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> Page[ChangelogRead]:
    rows, total = await service.list(limit=size, offset=(page - 1) * size)
    return Page[ChangelogRead](
        items=[ChangelogRead.model_validate(r) for r in rows],
        page=page,
        size=size,
        total=total,
    )


@router.post("", response_model=ChangelogRead, status_code=status.HTTP_201_CREATED)
async def publish_changelog(
    payload: ChangelogCreate, caller: CurrentUserDep, service: ChangelogServiceDep
) -> ChangelogRead:
    return ChangelogRead.model_validate(await service.create(caller, payload))


@router.patch("/{entry_id}", response_model=ChangelogRead)
async def update_changelog(
    entry_id: uuid.UUID,
    payload: ChangelogUpdate,
    caller: CurrentUserDep,
    service: ChangelogServiceDep,
) -> ChangelogRead:
    return ChangelogRead.model_validate(await service.update(caller, entry_id, payload))


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_changelog(
    entry_id: uuid.UUID, caller: CurrentUserDep, service: ChangelogServiceDep
) -> None:
    await service.delete(caller, entry_id)

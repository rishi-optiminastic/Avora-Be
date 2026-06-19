"""Work-entity (attribution catalog) endpoints — admin-only CRUD."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import CurrentUserDep, WorkEntityServiceDep
from app.schemas.work_entity import (
    WorkEntityCreate,
    WorkEntityOption,
    WorkEntityRead,
    WorkEntityUpdate,
)

router = APIRouter(prefix="/work-entities", tags=["work-entities"])


@router.get("", response_model=list[WorkEntityRead])
async def list_work_entities(
    caller: CurrentUserDep,
    service: WorkEntityServiceDep,
) -> list[WorkEntityRead]:
    return [WorkEntityRead.model_validate(e) for e in await service.list_all(caller)]


@router.get("/active", response_model=list[WorkEntityOption])
async def list_active_projects(
    caller: CurrentUserDep,
    service: WorkEntityServiceDep,
) -> list[WorkEntityOption]:
    """Active projects a manager can attach to a task (id + name + department)."""
    return [
        WorkEntityOption.model_validate(e) for e in await service.list_active_for_picker(caller)
    ]


@router.post("", response_model=WorkEntityRead, status_code=status.HTTP_201_CREATED)
async def create_work_entity(
    payload: WorkEntityCreate,
    caller: CurrentUserDep,
    service: WorkEntityServiceDep,
) -> WorkEntityRead:
    return WorkEntityRead.model_validate(await service.create(caller, payload))


@router.patch("/{entity_id}", response_model=WorkEntityRead)
async def update_work_entity(
    entity_id: uuid.UUID,
    payload: WorkEntityUpdate,
    caller: CurrentUserDep,
    service: WorkEntityServiceDep,
) -> WorkEntityRead:
    return WorkEntityRead.model_validate(await service.update(caller, entity_id, payload))


@router.delete("/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_work_entity(
    entity_id: uuid.UUID,
    caller: CurrentUserDep,
    service: WorkEntityServiceDep,
) -> None:
    await service.delete(caller, entity_id)

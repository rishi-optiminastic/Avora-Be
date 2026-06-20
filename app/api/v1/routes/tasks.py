"""Task endpoints — every read is scoped to the authenticated caller.

Routes only parse input, call the service, and return a response schema. No
business logic, no raw DB access (Layering §4). `response_model` is always set
and we return the schema, never the ORM object (Golden rule #5).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUserDep, TaskServiceDep
from app.models.task import TaskCadence, TaskStatus
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=Page[TaskRead])
async def list_tasks(
    caller: CurrentUserDep,
    service: TaskServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    cadence: Annotated[TaskCadence | None, Query()] = None,
    assignee_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[TaskRead]:
    offset = (page - 1) * size
    items, total = await service.list_for_caller(
        caller,
        offset=offset,
        limit=size,
        status=task_status,
        cadence=cadence,
        assignee_id=assignee_id,
    )
    return Page[TaskRead](
        items=[TaskRead.model_validate(t) for t in items],
        page=page,
        size=size,
        total=total,
    )


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    caller: CurrentUserDep,
    service: TaskServiceDep,
) -> TaskRead:
    task = await service.create(caller, payload)
    return TaskRead.model_validate(task)


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: uuid.UUID,
    caller: CurrentUserDep,
    service: TaskServiceDep,
) -> TaskRead:
    task = await service.get_for_caller(caller, task_id)
    return TaskRead.model_validate(task)


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    caller: CurrentUserDep,
    service: TaskServiceDep,
) -> TaskRead:
    task = await service.update(caller, task_id, payload)
    return TaskRead.model_validate(task)


@router.post("/{task_id}/escalate", response_model=TaskRead)
async def escalate_task(
    task_id: uuid.UUID,
    caller: CurrentUserDep,
    service: TaskServiceDep,
) -> TaskRead:
    """Flag a task for attention (manager/admin, scoped)."""
    return TaskRead.model_validate(await service.escalate(caller, task_id))


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: uuid.UUID,
    caller: CurrentUserDep,
    service: TaskServiceDep,
) -> None:
    await service.delete(caller, task_id)

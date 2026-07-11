"""Task endpoints — every read is scoped to the authenticated caller.

Routes only parse input, call the service, and return a response schema. No
business logic, no raw DB access (Layering §4). `response_model` is always set
and we return the schema, never the ORM object (Golden rule #5).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, status

from app.core.deps import (
    CommentRateLimitDep,
    CurrentUserDep,
    IdempotencyKeyHeader,
    IdempotencyServiceDep,
    TaskServiceDep,
)
from app.models.task import TaskCadence, TaskStatus
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.task import (
    CollaboratorAdd,
    TaskAppreciate,
    TaskBulkCreate,
    TaskCommentCreate,
    TaskCommentRead,
    TaskCreate,
    TaskParseRequest,
    TaskParseResult,
    TaskRead,
    TaskUpdate,
)

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
    idem: IdempotencyServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    async def _op() -> TaskRead:
        return TaskRead.model_validate(await service.create(caller, payload))

    return await idem.run(
        principal_id=caller.employee_id,
        scope="tasks.create",
        key=idempotency_key,
        request=payload,
        operation=_op,
        success_status=status.HTTP_201_CREATED,
    )


@router.post("/bulk", response_model=list[TaskRead], status_code=status.HTTP_201_CREATED)
async def create_tasks_bulk(
    payload: TaskBulkCreate,
    caller: CurrentUserDep,
    service: TaskServiceDep,
    idem: IdempotencyServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    """Create many tasks at once (paste-import). Manager/admin only; every
    assignee is authorized server-side. Idempotent on `Idempotency-Key` so a
    re-submitted paste can't create the whole batch twice."""

    async def _op() -> list[TaskRead]:
        tasks = await service.create_bulk(caller, payload)
        return [TaskRead.model_validate(t) for t in tasks]

    return await idem.run(
        principal_id=caller.employee_id,
        scope="tasks.bulk",
        key=idempotency_key,
        request=payload,
        operation=_op,
        success_status=status.HTTP_201_CREATED,
    )


@router.post("/parse", response_model=TaskParseResult)
async def parse_tasks(
    payload: TaskParseRequest,
    caller: CurrentUserDep,
    service: TaskServiceDep,
    idem: IdempotencyServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    """Parse a pasted blob into structured tasks (assignees matched to the
    caller's visible roster). Manager/admin only — nothing is created here.
    Idempotent on `Idempotency-Key` so a replay returns the first parse instead
    of paying for the LLM call again."""

    async def _op() -> TaskParseResult:
        return TaskParseResult(tasks=await service.parse_tasks(caller, payload.text))

    return await idem.run(
        principal_id=caller.employee_id,
        scope="tasks.parse",
        key=idempotency_key,
        request=payload,
        operation=_op,
    )


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


@router.post("/{task_id}/appreciate", response_model=TaskRead)
async def appreciate_task(
    task_id: uuid.UUID,
    payload: TaskAppreciate,
    caller: CurrentUserDep,
    service: TaskServiceDep,
) -> TaskRead:
    """Send the assignee kudos for completing a task (manager/assigner, scoped)."""
    return TaskRead.model_validate(await service.appreciate(caller, task_id, payload.note))


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: uuid.UUID,
    caller: CurrentUserDep,
    service: TaskServiceDep,
) -> None:
    await service.delete(caller, task_id)


@router.post("/{task_id}/collaborators", response_model=TaskRead)
async def add_task_collaborator(
    task_id: uuid.UUID,
    payload: CollaboratorAdd,
    caller: CurrentUserDep,
    service: TaskServiceDep,
) -> TaskRead:
    """Add a collaborator (view + comment-level). Allowed for the assigner, the
    assignee, or a manager/admin who can see the task and the added person."""
    task = await service.add_collaborator(caller, task_id, payload.employee_id)
    return TaskRead.model_validate(task)


@router.delete("/{task_id}/collaborators/{employee_id}", response_model=TaskRead)
async def remove_task_collaborator(
    task_id: uuid.UUID,
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: TaskServiceDep,
) -> TaskRead:
    """Remove a collaborator. Same authority as adding one."""
    task = await service.remove_collaborator(caller, task_id, employee_id)
    return TaskRead.model_validate(task)


@router.get("/{task_id}/comments", response_model=list[TaskCommentRead])
async def list_task_comments(
    task_id: uuid.UUID,
    caller: CurrentUserDep,
    service: TaskServiceDep,
) -> list[TaskCommentRead]:
    """The discussion thread on a task — visible to anyone who can see the task."""
    comments = await service.list_comments(caller, task_id)
    return [TaskCommentRead.model_validate(c) for c in comments]


@router.post(
    "/{task_id}/comments", response_model=TaskCommentRead, status_code=status.HTTP_201_CREATED
)
async def add_task_comment(
    task_id: uuid.UUID,
    payload: TaskCommentCreate,
    caller: CommentRateLimitDep,
    service: TaskServiceDep,
    idempotency_key: Annotated[str | None, Header(max_length=64)] = None,
) -> TaskCommentRead:
    """Rate-limited per user, and idempotent on the `Idempotency-Key` header so a
    copied/replayed request returns the original comment instead of a duplicate."""
    return TaskCommentRead.model_validate(
        await service.add_comment(caller, task_id, payload, idempotency_key=idempotency_key)
    )

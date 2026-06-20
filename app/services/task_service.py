"""Task business rules.

Reads are scoped in the repository. Writes are authorized here: you may assign a
task only to someone within your visible scope (reuses the employee scope), and
you may delete only a task you assigned (or as admin). No FastAPI objects here
(Layering §4); every mutation is audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.task import Task, TaskCadence, TaskStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.task import TaskRepository
from app.repositories.work_entity import WorkEntityRepository
from app.schemas.auth import CurrentUser
from app.schemas.task import TaskCreate, TaskUpdate

# An assignee updating their OWN task may report progress (move it on the board,
# update %, note blockers) — but not retitle, reassign, or write the review.
_ASSIGNEE_EDITABLE = frozenset(
    {"status", "remarks", "completion_pct", "blocked_reason", "attachments"}
)


class TaskService:
    def __init__(
        self,
        tasks: TaskRepository,
        employees: EmployeeRepository,
        entities: WorkEntityRepository,
        audit: AuditRepository,
    ) -> None:
        self._tasks = tasks
        self._employees = employees
        self._entities = entities
        self._audit = audit

    async def _validate_project(self, project_id: uuid.UUID | None) -> None:
        if project_id is None:
            return
        entity = await self._entities.get(project_id)
        if entity is None or not entity.is_active:
            raise ValidationError("Unknown or inactive project.")

    async def list_for_caller(
        self,
        caller: CurrentUser,
        *,
        offset: int,
        limit: int,
        status: TaskStatus | None = None,
        cadence: TaskCadence | None = None,
        assignee_id: uuid.UUID | None = None,
    ) -> tuple[Sequence[Task], int]:
        return await self._tasks.list_for_scope(
            caller,
            offset=offset,
            limit=limit,
            status=status,
            cadence=cadence,
            assignee_id=assignee_id,
        )

    async def get_for_caller(self, caller: CurrentUser, task_id: uuid.UUID) -> Task:
        task = await self._tasks.get_in_scope(caller, task_id)
        if task is None:
            raise NotFoundError()
        return task

    async def create(self, caller: CurrentUser, payload: TaskCreate) -> Task:
        # Only managers/senior managers/HR/admin create & assign tasks; an
        # individual contributor receives tasks, they don't author them.
        if not caller.is_manager:
            raise AuthorizationError()
        # You may only assign a task to someone you can see (reports, department,
        # or anyone for admin/HR) — reuses the employee scope.
        if not await self._employees.can_read(caller, payload.assignee_id):
            raise AuthorizationError()
        await self._validate_project(payload.project_id)
        task = await self._tasks.create(payload, assigned_by_id=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="task.create",
            target=f"task:{task.id}:assignee:{payload.assignee_id}",
        )
        return task

    async def update(self, caller: CurrentUser, task_id: uuid.UUID, payload: TaskUpdate) -> Task:
        task = await self._tasks.get_in_scope(caller, task_id)
        if task is None:
            raise NotFoundError()

        fields = payload.model_dump(exclude_unset=True)
        # A non-manager can only update tasks assigned TO them, and only their
        # status/remarks — never retitle, reassign, or change the project.
        if not caller.is_manager:
            if task.assignee_id != caller.employee_id or not set(fields).issubset(
                _ASSIGNEE_EDITABLE
            ):
                raise AuthorizationError()
        else:
            # Reassignment / re-project must stay valid and within scope.
            if payload.assignee_id is not None and not await self._employees.can_read(
                caller, payload.assignee_id
            ):
                raise AuthorizationError()
            if "project_id" in fields:
                await self._validate_project(payload.project_id)

        for key, value in fields.items():
            setattr(task, key, value)
        if "status" in fields:
            task.completed_at = datetime.now(UTC) if task.status is TaskStatus.DONE else None

        await self._tasks.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="task.update",
            target=f"task:{task.id}",
        )
        return task

    async def escalate(self, caller: CurrentUser, task_id: uuid.UUID) -> Task:
        """Flag a task for attention (overdue/blocked). Manager-only, scoped."""
        if not caller.is_manager:
            raise AuthorizationError()
        task = await self._tasks.get_in_scope(caller, task_id)
        if task is None:
            raise NotFoundError()
        task.escalated = True
        await self._tasks.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="task.escalate",
            target=f"task:{task.id}",
        )
        return task

    async def delete(self, caller: CurrentUser, task_id: uuid.UUID) -> None:
        task = await self._tasks.get(task_id)
        if task is None:
            raise NotFoundError()
        # Only the assigner (or an admin) may delete.
        if not caller.is_admin and task.assigned_by_id != caller.employee_id:
            raise AuthorizationError()
        await self._tasks.delete(task)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="task.delete",
            target=f"task:{task_id}",
        )

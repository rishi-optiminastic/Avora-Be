"""Task business rules.

Reads are scoped in the repository. Writes are authorized here: you may assign a
task only to someone within your visible scope (reuses the employee scope), and
you may delete only a task you assigned (or as admin). No FastAPI objects here
(Layering §4); every mutation is audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.notification import NotificationKind
from app.models.task import Task, TaskCadence, TaskStatus
from app.models.task_comment import TaskComment
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.task import TaskRepository
from app.repositories.task_comment import TaskCommentRepository
from app.repositories.work_entity import WorkEntityRepository
from app.schemas.auth import CurrentUser
from app.schemas.task import TaskCommentCreate, TaskCreate, TaskUpdate
from app.services.email_service import EmailError, EmailService
from app.services.notification_service import NotificationService

logger = get_logger("app.task")

# An assignee updating their OWN task may report progress (move it on the board,
# update %, note blockers) — but not retitle, reassign, or write the review.
_ASSIGNEE_EDITABLE = frozenset(
    {"status", "remarks", "completion_pct", "blocked_reason", "attachments"}
)

# Window in which an identical comment from the same author is treated as a
# duplicate of the first (idempotent replay protection on the discussion thread).
_COMMENT_DEDUP_WINDOW = timedelta(seconds=30)


class TaskService:
    def __init__(
        self,
        tasks: TaskRepository,
        employees: EmployeeRepository,
        entities: WorkEntityRepository,
        comments: TaskCommentRepository,
        audit: AuditRepository,
        notifications: NotificationService,
        email: EmailService,
    ) -> None:
        self._tasks = tasks
        self._employees = employees
        self._entities = entities
        self._comments = comments
        self._audit = audit
        self._notifications = notifications
        self._email = email

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
        # Let the assignee know (no-op if a manager assigns a task to themselves).
        notified = await self._notifications.notify(
            recipient_id=payload.assignee_id,
            kind=NotificationKind.TASK_ASSIGNED,
            title=f"New task assigned: {payload.title}",
            link=f"/dashboard/goals/tasks?task={task.id}",
            entity_type="task",
            entity_id=task.id,
            actor_id=caller.employee_id,
        )
        # Mirror to email only when a notification was actually delivered (i.e.
        # not a self-assignment, which notify() drops).
        if notified is not None:
            await self._email_assignment(task, caller)
        return task

    async def _email_assignment(self, task: Task, caller: CurrentUser) -> None:
        """Email the assignee about their new task. Best-effort: a delivery
        failure must never roll back the assignment, so we swallow and log it."""
        recipient = await self._employees.get(task.assignee_id)
        assigner = await self._employees.get(caller.employee_id)
        if recipient is None:
            return
        assigned_by = assigner.full_name if assigner is not None else "Your manager"
        due_label = f"{task.due_date:%d %b %Y}" if task.due_date is not None else None
        try:
            await self._email.send_task_assigned(
                to=recipient.work_email,
                employee_name=recipient.full_name,
                task_title=task.title,
                assigned_by=assigned_by,
                due_label=due_label,
                link_path=f"/dashboard/goals/tasks?task={task.id}",
            )
        except EmailError:
            logger.warning("task_assigned_email_failed", extra={"task_id": str(task.id)})

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

    # -- Discussion thread ---------------------------------------------------- #
    async def list_comments(self, caller: CurrentUser, task_id: uuid.UUID) -> Sequence[TaskComment]:
        # Only people who can see the task may read its thread (404 otherwise so
        # we never leak that the task exists — mirrors the leave thread).
        if await self._tasks.get_in_scope(caller, task_id) is None:
            raise NotFoundError()
        return await self._comments.list_for_task(task_id)

    async def add_comment(
        self,
        caller: CurrentUser,
        task_id: uuid.UUID,
        payload: TaskCommentCreate,
        *,
        idempotency_key: str | None = None,
    ) -> TaskComment:
        if await self._tasks.get_in_scope(caller, task_id) is None:
            raise NotFoundError()
        author_id = caller.employee_id

        # Idempotency. The client sends a fresh key per logical "send"; a replayed or
        # copied request carries the SAME key, so we return the original comment
        # instead of inserting a duplicate — a looped curl can't fill the thread.
        if idempotency_key is not None:
            prior = await self._comments.find_by_idempotency_key(
                author_id=author_id, idempotency_key=idempotency_key
            )
            if prior is not None:
                return prior
        else:
            # Keyless caller (old client / raw curl): fall back to a short content
            # window so an immediate double-submit still can't duplicate.
            prior = await self._comments.find_recent_duplicate(
                task_id=task_id,
                author_id=author_id,
                body=payload.body,
                since=datetime.now(UTC) - _COMMENT_DEDUP_WINDOW,
            )
            if prior is not None:
                return prior

        comment = await self._comments.insert_unique(
            task_id=task_id,
            author_id=author_id,
            body=payload.body,
            idempotency_key=idempotency_key,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="task.comment",
            target=f"task:{task_id}",
        )
        return comment

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
from app.schemas.task import (
    ParsedTask,
    TaskBulkCreate,
    TaskCommentCreate,
    TaskCreate,
    TaskUpdate,
)
from app.services.email_service import EmailError, EmailService
from app.services.llm_service import LlmError, LlmService
from app.services.notification_service import NotificationService

logger = get_logger("app.task")

# An assignee updating their OWN task may report progress (move it on the board,
# update %, note blockers) — but not retitle, reassign, or write the review.
# Collaborators get the same comment-level write surface (view + progress).
_ASSIGNEE_EDITABLE = frozenset(
    {"status", "remarks", "completion_pct", "blocked_reason", "attachments"}
)

# Window in which an identical comment from the same author is treated as a
# duplicate of the first (idempotent replay protection on the discussion thread).
_COMMENT_DEDUP_WINDOW = timedelta(seconds=30)

# At most one task-assignment EMAIL per recipient per this window — so assigning
# tasks one-by-one doesn't flood an inbox. The in-app notification still fires on
# every task; only the email is throttled (paste-import sends no email at all).
_ASSIGNMENT_EMAIL_WINDOW = timedelta(minutes=15)


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
        llm: LlmService,
    ) -> None:
        self._tasks = tasks
        self._employees = employees
        self._entities = entities
        self._comments = comments
        self._audit = audit
        self._notifications = notifications
        self._email = email
        self._llm = llm

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
        # not a self-assignment, which notify() drops) AND we haven't already
        # emailed this recipient about another task in the throttle window. The
        # count includes the notification we just created, so >1 means a prior
        # assignment already triggered an email — keeps one-by-one adds quiet.
        if notified is not None:
            recent = await self._notifications.recent_count(
                recipient_id=payload.assignee_id,
                kind=NotificationKind.TASK_ASSIGNED,
                within=_ASSIGNMENT_EMAIL_WINDOW,
            )
            if recent <= 1:
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

    async def create_bulk(self, caller: CurrentUser, payload: TaskBulkCreate) -> list[Task]:
        """Create many tasks at once (the paste-import flow). Manager-only, and
        every assignee is authorized up front — one out-of-scope assignee fails
        the whole batch (atomic; nothing is created)."""
        if not caller.is_manager:
            raise AuthorizationError()
        for item in payload.tasks:
            if not await self._employees.can_read(caller, item.assignee_id):
                raise AuthorizationError()
            await self._validate_project(item.project_id)

        tasks = await self._tasks.create_many(payload.tasks, assigned_by_id=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="task.create_bulk",
            target=f"count:{len(tasks)}",
        )
        # In-app notify each assignee (self-assignments drop). No email — a paste
        # of 30 tasks must not fire 30 emails.
        for task in tasks:
            await self._notifications.notify(
                recipient_id=task.assignee_id,
                kind=NotificationKind.TASK_ASSIGNED,
                title=f"New task assigned: {task.title}",
                link=f"/dashboard/goals/tasks?task={task.id}",
                entity_type="task",
                entity_id=task.id,
                actor_id=caller.employee_id,
            )
        return tasks

    async def parse_tasks(self, caller: CurrentUser, text: str) -> list[ParsedTask]:
        """Turn a pasted blob into structured tasks via the LLM. Manager-only; the
        model only ever sees the caller's VISIBLE roster, so it can't resolve a
        name to anyone outside the caller's scope."""
        if not caller.is_manager:
            raise AuthorizationError()
        if not self._llm.task_parse_ready:
            raise ValidationError("Task parsing isn't configured.")
        roster = [(e.id, e.full_name) for e in await self._employees.all_in_scope(caller)]
        try:
            return await self._llm.parse_tasks(text, roster)
        except LlmError as exc:
            logger.warning("task_parse_failed")
            raise ValidationError(
                "Couldn't read those tasks. Try again or add them manually."
            ) from exc

    async def update(self, caller: CurrentUser, task_id: uuid.UUID, payload: TaskUpdate) -> Task:
        task = await self._tasks.get_in_scope(caller, task_id)
        if task is None:
            raise NotFoundError()

        fields = payload.model_dump(exclude_unset=True)
        # A non-manager can only report progress (status/remarks/…) on a task they
        # own OR collaborate on — never retitle, reassign, or change the project.
        if not caller.is_manager:
            may_report = (
                task.assignee_id == caller.employee_id
                or await self._tasks.is_collaborator(task_id, caller.employee_id)
            )
            if not may_report or not set(fields).issubset(_ASSIGNEE_EDITABLE):
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

    # -- Collaborators -------------------------------------------------------- #
    async def _task_for_collaborator_change(self, caller: CurrentUser, task_id: uuid.UUID) -> Task:
        """Fetch a task the caller may change the collaborators of. Only the
        assigner, the assignee, or a manager/admin who can see it qualifies."""
        task = await self._tasks.get_in_scope(caller, task_id)
        if task is None:
            raise NotFoundError()
        if not (
            caller.is_manager
            or task.assigned_by_id == caller.employee_id
            or task.assignee_id == caller.employee_id
        ):
            raise AuthorizationError()
        return task

    async def add_collaborator(
        self, caller: CurrentUser, task_id: uuid.UUID, employee_id: uuid.UUID
    ) -> Task:
        task = await self._task_for_collaborator_change(caller, task_id)
        # The person added must be someone the caller can see.
        if not await self._employees.can_read(caller, employee_id):
            raise AuthorizationError()
        await self._tasks.add_collaborator(task_id, employee_id)
        await self._tasks.reload_collaborators(task)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="task.collaborator.add",
            target=f"task:{task_id}:employee:{employee_id}",
        )
        # Tell the new collaborator (self-add is dropped by notify()).
        await self._notifications.notify(
            recipient_id=employee_id,
            kind=NotificationKind.TASK_ASSIGNED,
            title=f"Added as a collaborator: {task.title}",
            link=f"/dashboard/goals/tasks?task={task.id}",
            entity_type="task",
            entity_id=task.id,
            actor_id=caller.employee_id,
        )
        return task

    async def remove_collaborator(
        self, caller: CurrentUser, task_id: uuid.UUID, employee_id: uuid.UUID
    ) -> Task:
        task = await self._task_for_collaborator_change(caller, task_id)
        await self._tasks.remove_collaborator(task_id, employee_id)
        await self._tasks.reload_collaborators(task)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="task.collaborator.remove",
            target=f"task:{task_id}:employee:{employee_id}",
        )
        return task

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

"""Task data access — the only place task queries are built.

Row-level scope mirrors the employee rules (Security rule 5.3), applied to the
task's assignee: you see your own tasks, your reports'/department's tasks, or
everything (admin/HR) — plus anything you assigned. Scope is derived from the
caller's server-side record, never a client field.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.employee import Employee, Role
from app.models.task import Task, TaskCadence, TaskStatus
from app.models.task_collaborator import TaskCollaborator
from app.schemas.auth import CurrentUser
from app.schemas.task import TaskCreate


@dataclass(frozen=True)
class ProjectManpowerRow:
    project_id: uuid.UUID
    people: int
    open_tasks: int
    total_tasks: int


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _scope_clause(self, caller: CurrentUser) -> ColumnElement[bool] | None:
        """What tasks may THIS caller read? Returns None for unrestricted.

        Everyone additionally sees any task they collaborate on (view +
        comment-level access) — that membership is OR-ed into every role's clause.
        """
        if caller.role in (Role.ADMIN, Role.HR):
            return None

        assigned_by_me = Task.assigned_by_id == caller.employee_id
        # Tasks the caller is a collaborator on — read access regardless of role.
        i_collaborate = Task.id.in_(
            select(TaskCollaborator.task_id).where(
                TaskCollaborator.employee_id == caller.employee_id
            )
        )

        if caller.role is Role.SENIOR_MANAGER:
            caller_dept = (
                select(Employee.department)
                .where(Employee.id == caller.employee_id)
                .scalar_subquery()
            )
            assignee_dept = (
                select(Employee.department).where(Employee.id == Task.assignee_id).scalar_subquery()
            )
            return (assignee_dept == caller_dept) | assigned_by_me | i_collaborate

        if caller.role is Role.MANAGER:
            assignee_manager = (
                select(Employee.manager_id).where(Employee.id == Task.assignee_id).scalar_subquery()
            )
            return (
                (Task.assignee_id == caller.employee_id)
                | (assignee_manager == caller.employee_id)
                | assigned_by_me
                | i_collaborate
            )

        # executive / it_admin / viewer / employee: own tasks (+ anything they
        # assigned, + anything they collaborate on).
        return (Task.assignee_id == caller.employee_id) | assigned_by_me | i_collaborate

    async def create(self, payload: TaskCreate, *, assigned_by_id: uuid.UUID) -> Task:
        task = Task(
            title=payload.title,
            description=payload.description,
            assignee_id=payload.assignee_id,
            assigned_by_id=assigned_by_id,
            project=payload.project,
            project_id=payload.project_id,
            priority=payload.priority,
            cadence=payload.cadence,
            start_date=payload.start_date,
            due_date=payload.due_date,
            remarks=payload.remarks,
            expected_output=payload.expected_output,
            attachments=[a.model_dump() for a in payload.attachments],
            parent_task_id=payload.parent_task_id,
            depends_on_id=payload.depends_on_id,
        )
        self._session.add(task)
        await self._session.flush()
        # A new task has no collaborators yet — seed the (selectin) collection so
        # serializing `collaborator_ids` never triggers a lazy load outside async.
        set_committed_value(task, "collaborators", [])
        return task

    async def create_many(
        self, payloads: Sequence[TaskCreate], *, assigned_by_id: uuid.UUID
    ) -> list[Task]:
        """Insert a batch of tasks (paste-import). Authorization of each assignee
        happens in the service before this is called."""
        tasks = [
            Task(
                title=payload.title,
                description=payload.description,
                assignee_id=payload.assignee_id,
                assigned_by_id=assigned_by_id,
                project=payload.project,
                project_id=payload.project_id,
                priority=payload.priority,
                cadence=payload.cadence,
                start_date=payload.start_date,
                due_date=payload.due_date,
                remarks=payload.remarks,
                expected_output=payload.expected_output,
                attachments=[a.model_dump() for a in payload.attachments],
                parent_task_id=payload.parent_task_id,
                depends_on_id=payload.depends_on_id,
            )
            for payload in payloads
        ]
        self._session.add_all(tasks)
        await self._session.flush()
        for task in tasks:
            set_committed_value(task, "collaborators", [])
        return tasks

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return await self._session.get(Task, task_id)

    async def is_collaborator(self, task_id: uuid.UUID, employee_id: uuid.UUID) -> bool:
        row = await self._session.get(TaskCollaborator, (task_id, employee_id))
        return row is not None

    async def add_collaborator(self, task_id: uuid.UUID, employee_id: uuid.UUID) -> None:
        """Idempotent — adding an existing collaborator is a no-op."""
        if await self.is_collaborator(task_id, employee_id):
            return
        self._session.add(TaskCollaborator(task_id=task_id, employee_id=employee_id))
        await self._session.flush()

    async def remove_collaborator(self, task_id: uuid.UUID, employee_id: uuid.UUID) -> None:
        row = await self._session.get(TaskCollaborator, (task_id, employee_id))
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()

    async def reload_collaborators(self, task: Task) -> None:
        """Re-read a task's collaborators after a membership change (the relation
        is read-only/secondary, so it doesn't auto-sync with add/remove)."""
        await self._session.refresh(task, attribute_names=["collaborators"])

    async def get_in_scope(self, caller: CurrentUser, task_id: uuid.UUID) -> Task | None:
        clause = self._scope_clause(caller)
        stmt = select(Task).where(Task.id == task_id)
        if clause is not None:
            stmt = stmt.where(clause)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_scope(
        self,
        caller: CurrentUser,
        *,
        offset: int,
        limit: int,
        status: TaskStatus | None = None,
        cadence: TaskCadence | None = None,
        assignee_id: uuid.UUID | None = None,
    ) -> tuple[Sequence[Task], int]:
        stmt = select(Task)
        clause = self._scope_clause(caller)
        if clause is not None:
            stmt = stmt.where(clause)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        if cadence is not None:
            stmt = stmt.where(Task.cadence == cadence)
        if assignee_id is not None:
            stmt = stmt.where(Task.assignee_id == assignee_id)

        total = await self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = await self._session.execute(
            stmt.order_by(Task.due_date.is_(None), Task.due_date.asc(), Task.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return rows.scalars().all(), int(total or 0)

    async def list_overdue(self, caller: CurrentUser, now: datetime) -> Sequence[Task]:
        """In-scope tasks past their due date and not yet done (delayed rollup)."""
        stmt = select(Task).where(
            Task.due_date.is_not(None),
            Task.due_date < now,
            Task.status != TaskStatus.DONE,
        )
        clause = self._scope_clause(caller)
        if clause is not None:
            stmt = stmt.where(clause)
        rows = await self._session.execute(stmt.order_by(Task.due_date.asc()))
        return rows.scalars().all()

    async def count_done_total(self, caller: CurrentUser) -> tuple[int, int]:
        """(#done, #total) tasks in the caller's scope — the Overview KPI."""
        stmt = select(
            func.count().filter(Task.status == TaskStatus.DONE),
            func.count(),
        )
        clause = self._scope_clause(caller)
        if clause is not None:
            stmt = stmt.where(clause)
        row = (await self._session.execute(stmt)).one()
        return int(row[0] or 0), int(row[1] or 0)

    async def manpower_by_project(self, caller: CurrentUser) -> list[ProjectManpowerRow]:
        """Per-project headcount + open-task count, scoped to the caller."""
        stmt = select(
            Task.project_id,
            func.count(func.distinct(Task.assignee_id)),
            func.count().filter(Task.status != TaskStatus.DONE),
            func.count(),
        ).where(Task.project_id.is_not(None))
        clause = self._scope_clause(caller)
        if clause is not None:
            stmt = stmt.where(clause)
        rows = await self._session.execute(stmt.group_by(Task.project_id))
        return [
            ProjectManpowerRow(
                project_id=pid, people=people, open_tasks=open_tasks, total_tasks=total
            )
            for pid, people, open_tasks, total in rows.all()
            if pid is not None
        ]

    async def flush(self) -> None:
        """Persist in-place mutations the service made on a fetched Task."""
        await self._session.flush()

    async def delete(self, task: Task) -> None:
        await self._session.delete(task)
        await self._session.flush()

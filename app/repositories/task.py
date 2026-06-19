"""Task data access — the only place task queries are built.

Row-level scope mirrors the employee rules (Security rule 5.3), applied to the
task's assignee: you see your own tasks, your reports'/department's tasks, or
everything (admin/HR) — plus anything you assigned. Scope is derived from the
caller's server-side record, never a client field.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, Role
from app.models.task import Task, TaskCadence, TaskStatus
from app.schemas.auth import CurrentUser
from app.schemas.task import TaskCreate


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _scope_clause(self, caller: CurrentUser) -> ColumnElement[bool] | None:
        """What tasks may THIS caller read? Returns None for unrestricted."""
        if caller.role in (Role.ADMIN, Role.HR):
            return None

        assigned_by_me = Task.assigned_by_id == caller.employee_id

        if caller.role is Role.SENIOR_MANAGER:
            caller_dept = (
                select(Employee.department)
                .where(Employee.id == caller.employee_id)
                .scalar_subquery()
            )
            assignee_dept = (
                select(Employee.department).where(Employee.id == Task.assignee_id).scalar_subquery()
            )
            return (assignee_dept == caller_dept) | assigned_by_me

        if caller.role is Role.MANAGER:
            assignee_manager = (
                select(Employee.manager_id).where(Employee.id == Task.assignee_id).scalar_subquery()
            )
            return (
                (Task.assignee_id == caller.employee_id)
                | (assignee_manager == caller.employee_id)
                | assigned_by_me
            )

        # executive / it_admin / viewer / employee: own tasks (+ anything they assigned).
        return (Task.assignee_id == caller.employee_id) | assigned_by_me

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
        )
        self._session.add(task)
        await self._session.flush()
        return task

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return await self._session.get(Task, task_id)

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

    async def flush(self) -> None:
        """Persist in-place mutations the service made on a fetched Task."""
        await self._session.flush()

    async def delete(self, task: Task) -> None:
        await self._session.delete(task)
        await self._session.flush()

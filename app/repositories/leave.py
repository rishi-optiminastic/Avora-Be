"""Leave data access — the only place leave queries are built.

Row-level scope mirrors employees/tasks (Security rule 5.3), on the requesting
employee: your own requests, your reports'/department's, or everything
(admin/HR), plus anything you are the reviewer of.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, Role
from app.models.leave import Leave, LeaveStatus
from app.models.leave_comment import LeaveComment
from app.schemas.auth import CurrentUser
from app.schemas.leave import LeaveCreate


class LeaveRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _scope_clause(self, caller: CurrentUser) -> ColumnElement[bool] | None:
        if caller.role in (Role.ADMIN, Role.HR):
            return None
        reviewed_by_me = Leave.reviewer_id == caller.employee_id
        if caller.role is Role.SENIOR_MANAGER:
            caller_dept = (
                select(Employee.department)
                .where(Employee.id == caller.employee_id)
                .scalar_subquery()
            )
            requester_dept = (
                select(Employee.department)
                .where(Employee.id == Leave.employee_id)
                .scalar_subquery()
            )
            return (requester_dept == caller_dept) | reviewed_by_me
        if caller.role is Role.MANAGER:
            requester_manager = (
                select(Employee.manager_id)
                .where(Employee.id == Leave.employee_id)
                .scalar_subquery()
            )
            return (
                (Leave.employee_id == caller.employee_id)
                | (requester_manager == caller.employee_id)
                | reviewed_by_me
            )
        return (Leave.employee_id == caller.employee_id) | reviewed_by_me

    async def create(
        self, payload: LeaveCreate, *, employee_id: uuid.UUID, reviewer_id: uuid.UUID | None
    ) -> Leave:
        leave = Leave(
            employee_id=employee_id,
            leave_type=payload.leave_type,
            start_date=payload.start_date,
            end_date=payload.end_date,
            reason=payload.reason,
            reviewer_id=reviewer_id,
            status=LeaveStatus.SUBMITTED,
        )
        self._session.add(leave)
        await self._session.flush()
        return leave

    async def get(self, leave_id: uuid.UUID) -> Leave | None:
        return await self._session.get(Leave, leave_id)

    async def get_in_scope(self, caller: CurrentUser, leave_id: uuid.UUID) -> Leave | None:
        clause = self._scope_clause(caller)
        stmt = select(Leave).where(Leave.id == leave_id)
        if clause is not None:
            stmt = stmt.where(clause)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_scope(
        self,
        caller: CurrentUser,
        *,
        offset: int,
        limit: int,
        status: LeaveStatus | None = None,
    ) -> tuple[Sequence[tuple[Leave, int]], int]:
        """Returns (leave, comment_count) pairs so the list can badge threads."""
        comment_count = (
            select(func.count(LeaveComment.id))
            .where(LeaveComment.leave_id == Leave.id)
            .correlate(Leave)
            .scalar_subquery()
        )
        stmt = select(Leave, comment_count.label("comment_count"))
        clause = self._scope_clause(caller)
        if clause is not None:
            stmt = stmt.where(clause)
        if status is not None:
            stmt = stmt.where(Leave.status == status)
        total = await self._session.scalar(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        )
        rows = await self._session.execute(
            stmt.order_by(Leave.start_date.desc()).offset(offset).limit(limit)
        )
        return [(row[0], int(row[1])) for row in rows.all()], int(total or 0)

    async def flush(self) -> None:
        await self._session.flush()

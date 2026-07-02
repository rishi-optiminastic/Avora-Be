"""Leave data access — the only place leave queries are built.

Row-level scope mirrors employees/tasks (Security rule 5.3), on the requesting
employee: your own requests, your reports'/department's, or everything
(admin/HR), plus anything you are the reviewer of.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, Role
from app.models.leave import Leave, LeaveStatus, LeaveType
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
            half_day_period=payload.half_day_period,
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

    async def approved_paid_in_range(
        self, employee_ids: Sequence[uuid.UUID], start: datetime, end: datetime
    ) -> dict[uuid.UUID, list[tuple[datetime, datetime, LeaveType]]]:
        """Approved, *paid* leaves overlapping [start, end) per employee.

        Unpaid (LWP) leave is excluded — it does not count toward payable days.
        Payroll (HR/Admin) is the only caller, so this is org-wide and not
        scope-clamped; the service authorizes before calling in.
        """
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(Leave.employee_id, Leave.start_date, Leave.end_date, Leave.leave_type).where(
                Leave.employee_id.in_(employee_ids),
                Leave.status == LeaveStatus.APPROVED,
                Leave.leave_type != LeaveType.UNPAID,
                Leave.start_date < end,
                Leave.end_date >= start,
            )
        )
        out: dict[uuid.UUID, list[tuple[datetime, datetime, LeaveType]]] = {}
        for emp_id, s, e, kind in rows.all():
            out.setdefault(emp_id, []).append((s, e, kind))
        return out

    async def for_employee_in_window(
        self,
        employee_id: uuid.UUID,
        start: datetime,
        end: datetime,
        statuses: Sequence[LeaveStatus],
    ) -> list[tuple[datetime, datetime, LeaveType, LeaveStatus]]:
        """One employee's leaves overlapping [start, end) in the given statuses.

        Used by the balance calc (APPROVED = used, SUBMITTED = pending). The
        caller is the employee themselves or an authorised viewer — the service
        checks visibility before calling in, so this is not scope-clamped.
        """
        if not statuses:
            return []
        rows = await self._session.execute(
            select(Leave.start_date, Leave.end_date, Leave.leave_type, Leave.status).where(
                Leave.employee_id == employee_id,
                Leave.status.in_(statuses),
                Leave.start_date < end,
                Leave.end_date >= start,
            )
        )
        return [(s, e, kind, status) for s, e, kind, status in rows.all()]

    async def count_on_leave(
        self, employee_ids: Sequence[uuid.UUID], start: datetime, end: datetime
    ) -> int:
        """How many of these employees have an approved leave overlapping [start, end).

        Counts any leave type (paid or unpaid) — they are all "out of office" for the
        Overview headcount. Caller scopes `employee_ids` before passing them in.
        """
        if not employee_ids:
            return 0
        total = await self._session.scalar(
            select(func.count(func.distinct(Leave.employee_id))).where(
                Leave.employee_id.in_(employee_ids),
                Leave.status == LeaveStatus.APPROVED,
                Leave.start_date < end,
                Leave.end_date >= start,
            )
        )
        return int(total or 0)

    async def flush(self) -> None:
        await self._session.flush()

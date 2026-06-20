"""Target data access — the only place target queries are built.

Row-level scope (Security rule 5.3): admin/HR see all; a senior manager sees
their department's targets (plus any they assigned or own); a manager sees
targets they own or assigned; everyone else sees only their own. Scope is
derived from the caller's server-side record, never a client field.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, Role
from app.models.target import Target, TargetPeriod, TargetStatus
from app.schemas.auth import CurrentUser
from app.schemas.target import TargetCreate


class TargetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _scope_clause(self, caller: CurrentUser) -> ColumnElement[bool] | None:
        if caller.role in (Role.ADMIN, Role.HR):
            return None
        mine = (Target.owner_id == caller.employee_id) | (
            Target.assigned_by_id == caller.employee_id
        )
        if caller.role is Role.SENIOR_MANAGER:
            caller_dept = (
                select(Employee.department)
                .where(Employee.id == caller.employee_id)
                .scalar_subquery()
            )
            return (Target.department == caller_dept) | mine
        return mine

    async def create(self, payload: TargetCreate, *, assigned_by_id: uuid.UUID) -> Target:
        target = Target(
            title=payload.title,
            description=payload.description,
            period=payload.period,
            department=payload.department,
            owner_id=payload.owner_id,
            assigned_by_id=assigned_by_id,
            metric=payload.metric,
            unit=payload.unit,
            target_value=payload.target_value,
            start_date=payload.start_date,
            due_date=payload.due_date,
            expected_result=payload.expected_result,
            member_ids=[str(m) for m in payload.member_ids],
            parent_target_id=payload.parent_target_id,
        )
        self._session.add(target)
        await self._session.flush()
        return target

    async def get(self, target_id: uuid.UUID) -> Target | None:
        return await self._session.get(Target, target_id)

    async def get_in_scope(self, caller: CurrentUser, target_id: uuid.UUID) -> Target | None:
        stmt = select(Target).where(Target.id == target_id)
        clause = self._scope_clause(caller)
        if clause is not None:
            stmt = stmt.where(clause)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_scope(
        self,
        caller: CurrentUser,
        *,
        offset: int,
        limit: int,
        period: TargetPeriod | None = None,
        status: TargetStatus | None = None,
    ) -> tuple[Sequence[Target], int]:
        stmt = select(Target)
        clause = self._scope_clause(caller)
        if clause is not None:
            stmt = stmt.where(clause)
        if period is not None:
            stmt = stmt.where(Target.period == period)
        if status is not None:
            stmt = stmt.where(Target.status == status)

        total = await self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = await self._session.execute(
            stmt.order_by(
                Target.due_date.is_(None), Target.due_date.asc(), Target.created_at.desc()
            )
            .offset(offset)
            .limit(limit)
        )
        return rows.scalars().all(), int(total or 0)

    async def flush(self) -> None:
        await self._session.flush()

    async def delete(self, target: Target) -> None:
        await self._session.delete(target)
        await self._session.flush()

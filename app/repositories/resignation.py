"""Resignation data access — the only place resignation queries are built.

Row scope: an employee sees their own; HR/Admin see everyone's. Scope is derived
from the caller's server-side role, never a client field (Golden rule #3).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Role
from app.models.resignation import Resignation, ResignationStatus
from app.schemas.auth import CurrentUser
from app.schemas.resignation import ResignationCreate


class ResignationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _scope_clause(self, caller: CurrentUser) -> ColumnElement[bool] | None:
        """None ⇒ unrestricted (HR/Admin). Everyone else sees only their own."""
        if caller.role in (Role.ADMIN, Role.HR):
            return None
        return Resignation.employee_id == caller.employee_id

    async def create(self, payload: ResignationCreate, *, employee_id: uuid.UUID) -> Resignation:
        row = Resignation(
            employee_id=employee_id,
            reason=payload.reason,
            last_working_day=payload.last_working_day,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, resignation_id: uuid.UUID) -> Resignation | None:
        return await self._session.get(Resignation, resignation_id)

    async def get_in_scope(
        self, caller: CurrentUser, resignation_id: uuid.UUID
    ) -> Resignation | None:
        stmt = select(Resignation).where(Resignation.id == resignation_id)
        clause = self._scope_clause(caller)
        if clause is not None:
            stmt = stmt.where(clause)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def has_active(self, employee_id: uuid.UUID) -> bool:
        """A pending (submitted) resignation already on file — used to block a
        second one while the first is undecided."""
        stmt = select(func.count()).where(
            Resignation.employee_id == employee_id,
            Resignation.status == ResignationStatus.SUBMITTED,
        )
        return bool(await self._session.scalar(stmt))

    async def list_for_scope(
        self, caller: CurrentUser, *, offset: int, limit: int
    ) -> tuple[Sequence[Resignation], int]:
        stmt = select(Resignation)
        clause = self._scope_clause(caller)
        if clause is not None:
            stmt = stmt.where(clause)
        total = await self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = await self._session.execute(
            stmt.order_by(Resignation.created_at.desc()).offset(offset).limit(limit)
        )
        return rows.scalars().all(), int(total or 0)

    async def flush(self) -> None:
        await self._session.flush()

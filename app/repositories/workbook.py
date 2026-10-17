"""Workbook data access. The visibility rule lives in `visible_clause` so the
list and the single read cannot disagree about who may see a sheet."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.workbook import Workbook, WorkbookSharing


def visible_clause(employee_id: uuid.UUID, *, is_admin: bool) -> ColumnElement[bool]:
    """Own sheets, shared sheets, or every sheet for an admin."""
    if is_admin:
        return Workbook.id.is_not(None)
    return or_(Workbook.owner_id == employee_id, Workbook.sharing != WorkbookSharing.PRIVATE)


class WorkbookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_visible(
        self, employee_id: uuid.UUID, *, is_admin: bool, offset: int, limit: int
    ) -> tuple[Sequence[Workbook], int]:
        where = visible_clause(employee_id, is_admin=is_admin)
        total = await self._session.scalar(select(func.count()).select_from(Workbook).where(where))
        rows = await self._session.scalars(
            select(Workbook)
            .where(where)
            .order_by(Workbook.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(rows), int(total or 0)

    async def get(self, workbook_id: uuid.UUID) -> Workbook | None:
        return await self._session.get(Workbook, workbook_id)

    async def get_for_update(self, workbook_id: uuid.UUID) -> Workbook | None:
        """Row-locked read, so two saves of the same version cannot both win."""
        row: Workbook | None = await self._session.scalar(
            select(Workbook).where(Workbook.id == workbook_id).with_for_update()
        )
        return row

    async def names(self, ids: Sequence[uuid.UUID | None]) -> dict[uuid.UUID, str]:
        wanted = {i for i in ids if i is not None}
        if not wanted:
            return {}
        rows = await self._session.execute(
            select(Employee.id, Employee.full_name).where(Employee.id.in_(wanted))
        )
        return dict(rows.tuples().all())

    def add(self, workbook: Workbook) -> None:
        self._session.add(workbook)

    async def delete(self, workbook: Workbook) -> None:
        await self._session.delete(workbook)
        await self._session.flush()

    async def flush(self) -> None:
        await self._session.flush()

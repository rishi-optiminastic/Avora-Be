"""Workbook data access. The visibility rule lives in `visible_clause` so the
list and the single read cannot disagree about who may see a sheet."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.workbook import Workbook, WorkbookAccess, WorkbookMember, WorkbookSharing

# How many people a share-search returns; the picker narrows as you type.
PEOPLE_SEARCH_LIMIT = 20


class SheetScope(StrEnum):
    ALL = "all"
    OWNED = "owned"
    SHARED = "shared"  # visible to me, owned by someone else


def _shared_with(employee_id: uuid.UUID) -> ColumnElement[bool]:
    named = select(WorkbookMember.workbook_id).where(WorkbookMember.employee_id == employee_id)
    return or_(Workbook.sharing != WorkbookSharing.PRIVATE, Workbook.id.in_(named))


def visible_clause(employee_id: uuid.UUID, *, is_admin: bool) -> ColumnElement[bool]:
    """Own sheets, sheets shared with everyone or with me by name; all for an admin."""
    if is_admin:
        return Workbook.id.is_not(None)
    return or_(Workbook.owner_id == employee_id, _shared_with(employee_id))


def _scope_clause(
    employee_id: uuid.UUID, scope: SheetScope, *, is_admin: bool
) -> ColumnElement[bool]:
    if scope is SheetScope.OWNED:
        return Workbook.owner_id == employee_id
    if scope is SheetScope.SHARED:
        return and_(Workbook.owner_id != employee_id, _shared_with(employee_id))
    return visible_clause(employee_id, is_admin=is_admin)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class WorkbookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_visible(
        self,
        employee_id: uuid.UUID,
        *,
        is_admin: bool,
        scope: SheetScope,
        query: str | None,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[Workbook], int]:
        where = _scope_clause(employee_id, scope, is_admin=is_admin)
        if query:
            where = and_(where, Workbook.title.ilike(f"%{_escape_like(query)}%", escape="\\"))
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

    # ------------------------------------------------------------- members

    async def access_of(
        self, workbook_ids: Sequence[uuid.UUID], employee_id: uuid.UUID
    ) -> dict[uuid.UUID, WorkbookAccess]:
        """The caller's named access to each of these sheets, in one query."""
        if not workbook_ids:
            return {}
        rows = await self._session.execute(
            select(WorkbookMember.workbook_id, WorkbookMember.access).where(
                WorkbookMember.workbook_id.in_(workbook_ids),
                WorkbookMember.employee_id == employee_id,
            )
        )
        return dict(rows.tuples().all())

    async def member_names(self, workbook_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
        """Who each sheet is shared with by name, for the list's avatars."""
        if not workbook_ids:
            return {}
        rows = await self._session.execute(
            select(WorkbookMember.workbook_id, Employee.full_name)
            .join(Employee, Employee.id == WorkbookMember.employee_id)
            .where(WorkbookMember.workbook_id.in_(workbook_ids))
            .order_by(WorkbookMember.created_at)
        )
        out: dict[uuid.UUID, list[str]] = defaultdict(list)
        for workbook_id, name in rows.tuples().all():
            out[workbook_id].append(name)
        return out

    async def members(self, workbook_id: uuid.UUID) -> list[tuple[WorkbookMember, Employee]]:
        rows = await self._session.execute(
            select(WorkbookMember, Employee)
            .join(Employee, Employee.id == WorkbookMember.employee_id)
            .where(WorkbookMember.workbook_id == workbook_id)
            .order_by(Employee.full_name)
        )
        return [(member, person) for member, person in rows.tuples().all()]

    async def member(self, workbook_id: uuid.UUID, employee_id: uuid.UUID) -> WorkbookMember | None:
        row: WorkbookMember | None = await self._session.scalar(
            select(WorkbookMember).where(
                WorkbookMember.workbook_id == workbook_id,
                WorkbookMember.employee_id == employee_id,
            )
        )
        return row

    async def search_people(self, query: str) -> Sequence[Employee]:
        """Active people whose name or work email matches, for the share picker."""
        pattern = f"%{_escape_like(query)}%"
        rows = await self._session.scalars(
            select(Employee)
            .where(
                Employee.is_active.is_(True),
                or_(
                    Employee.full_name.ilike(pattern, escape="\\"),
                    Employee.work_email.ilike(pattern, escape="\\"),
                ),
            )
            .order_by(Employee.full_name)
            .limit(PEOPLE_SEARCH_LIMIT)
        )
        return list(rows)

    async def active_employee(self, employee_id: uuid.UUID) -> Employee | None:
        person = await self._session.get(Employee, employee_id)
        return person if person is not None and person.is_active else None

    # ---------------------------------------------------------------- writes

    def add(self, row: Workbook | WorkbookMember) -> None:
        self._session.add(row)

    async def delete(self, row: Workbook | WorkbookMember) -> None:
        await self._session.delete(row)
        await self._session.flush()

    async def flush(self) -> None:
        await self._session.flush()

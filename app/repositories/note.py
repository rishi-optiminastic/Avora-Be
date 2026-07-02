"""Note data access — always scoped to one employee (the author).

No cross-employee scope clause needed: a note is either the caller's own (the
only case this repository is ever asked for) or not visible at all. The
service enforces authorship before calling `delete`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.note import Note

_LIST_LIMIT = 200


class NoteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, employee_id: uuid.UUID, body: str) -> Note:
        note = Note(employee_id=employee_id, body=body)
        self._session.add(note)
        await self._session.flush()
        return note

    async def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[Note]:
        rows = await self._session.execute(
            select(Note)
            .where(Note.employee_id == employee_id)
            .order_by(Note.created_at.desc())
            .limit(_LIST_LIMIT)
        )
        return rows.scalars().all()

    async def get(self, note_id: uuid.UUID) -> Note | None:
        return await self._session.get(Note, note_id)

    async def delete(self, note: Note) -> None:
        await self._session.delete(note)
        await self._session.flush()

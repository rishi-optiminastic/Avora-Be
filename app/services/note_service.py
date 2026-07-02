"""Note business rules — strictly author-only, no sharing.

Unlike compensation/leave, a personal note carries no org-sensitive data and
isn't a segregation-of-duties concern, so reads/writes aren't audited — it's
scoped to "yours only" and that's the whole policy.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import NotFoundError
from app.models.note import Note
from app.repositories.note import NoteRepository
from app.schemas.auth import CurrentUser
from app.schemas.note import NoteCreate


class NoteService:
    def __init__(self, notes: NoteRepository) -> None:
        self._notes = notes

    async def create(self, caller: CurrentUser, payload: NoteCreate) -> Note:
        return await self._notes.create(caller.employee_id, payload.body)

    async def list_mine(self, caller: CurrentUser) -> Sequence[Note]:
        return await self._notes.list_for_employee(caller.employee_id)

    async def delete(self, caller: CurrentUser, note_id: uuid.UUID) -> None:
        note = await self._notes.get(note_id)
        # 404, not 403 — a note's existence is never worth revealing to a
        # non-owner (§7), and there is no legitimate cross-employee case here.
        if note is None or note.employee_id != caller.employee_id:
            raise NotFoundError()
        await self._notes.delete(note)

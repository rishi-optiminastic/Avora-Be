"""Note endpoints — a personal quick-capture scratchpad, author-only.

Every route implicitly scopes to `caller.employee_id`; there is no notion of
another employee's notes here (Security rule 5.3 — the scope is just "mine").
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import CurrentUserDep, NoteServiceDep
from app.schemas.note import NoteCreate, NoteRead

router = APIRouter(prefix="/notes", tags=["notes"])


@router.get("", response_model=list[NoteRead])
async def list_notes(caller: CurrentUserDep, service: NoteServiceDep) -> list[NoteRead]:
    notes = await service.list_mine(caller)
    return [NoteRead.model_validate(n) for n in notes]


@router.post("", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
async def create_note(
    payload: NoteCreate, caller: CurrentUserDep, service: NoteServiceDep
) -> NoteRead:
    return NoteRead.model_validate(await service.create(caller, payload))


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: uuid.UUID, caller: CurrentUserDep, service: NoteServiceDep
) -> None:
    await service.delete(caller, note_id)

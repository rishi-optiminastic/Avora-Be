"""Sheets - spreadsheets kept in Avora.

Who may do what (checked here, never in the UI):
  view   - the owner, anyone when the sheet is shared, and admins.
  edit   - the owner, anyone when it is shared for editing, and admins.
           Viewers (a read-only role) never edit or create.
  manage - rename, reshare, delete: the owner and admins.

A sheet the caller may not view is a 404, so a private sheet's existence is not
revealed. Saves are optimistic: a save based on an old version is refused with a
409 naming who saved in between, and the editor then lets the person reload or
deliberately overwrite (`force`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.core.workbook_snapshot import scrub_links
from app.models.employee import Role
from app.models.workbook import Workbook, WorkbookSharing
from app.repositories.audit import AuditRepository
from app.repositories.workbook import WorkbookRepository
from app.schemas.auth import CurrentUser
from app.schemas.common import Page, PageParams
from app.schemas.workbook import (
    WorkbookContentSave,
    WorkbookCreate,
    WorkbookRead,
    WorkbookSaved,
    WorkbookSummary,
    WorkbookUpdate,
)


def may_view(caller: CurrentUser, workbook: Workbook) -> bool:
    return (
        caller.is_admin
        or workbook.owner_id == caller.employee_id
        or workbook.sharing is not WorkbookSharing.PRIVATE
    )


def may_edit(caller: CurrentUser, workbook: Workbook) -> bool:
    if caller.role is Role.VIEWER:
        return False
    return may_manage(caller, workbook) or workbook.sharing is WorkbookSharing.ORG_EDIT


def may_manage(caller: CurrentUser, workbook: Workbook) -> bool:
    return caller.is_admin or workbook.owner_id == caller.employee_id


class WorkbookService:
    def __init__(self, workbooks: WorkbookRepository, audit: AuditRepository) -> None:
        self._workbooks = workbooks
        self._audit = audit

    async def list(self, caller: CurrentUser, params: PageParams) -> Page[WorkbookSummary]:
        rows, total = await self._workbooks.list_visible(
            caller.employee_id, is_admin=caller.is_admin, offset=params.offset, limit=params.limit
        )
        names = await self._workbooks.names(
            [r.owner_id for r in rows] + [r.updated_by_id for r in rows]
        )
        items = [self._summary(caller, r, names) for r in rows]
        return Page(items=items, page=params.page, size=params.size, total=total)

    async def get(self, caller: CurrentUser, workbook_id: uuid.UUID) -> WorkbookRead:
        workbook = await self._visible(caller, workbook_id)
        return await self._read(caller, workbook)

    async def create(self, caller: CurrentUser, payload: WorkbookCreate) -> WorkbookRead:
        if caller.role is Role.VIEWER:
            raise AuthorizationError("Your role can view sheets but not create them.")
        workbook = Workbook(
            title=payload.title,
            owner_id=caller.employee_id,
            sharing=payload.sharing,
            snapshot={},
            version=1,
            updated_by_id=caller.employee_id,
        )
        self._workbooks.add(workbook)
        await self._workbooks.flush()
        await self._log(caller, "sheet.create", workbook)
        return await self._read(caller, workbook)

    async def update(
        self, caller: CurrentUser, workbook_id: uuid.UUID, payload: WorkbookUpdate
    ) -> WorkbookRead:
        workbook = await self._managed(caller, workbook_id)
        if payload.title is not None:
            workbook.title = payload.title
        if payload.sharing is not None and payload.sharing is not workbook.sharing:
            workbook.sharing = payload.sharing
            await self._log(caller, f"sheet.share.{payload.sharing.value}", workbook)
        await self._workbooks.flush()
        return await self._read(caller, workbook)

    async def save(
        self, caller: CurrentUser, workbook_id: uuid.UUID, payload: WorkbookContentSave
    ) -> WorkbookSaved:
        workbook = await self._workbooks.get_for_update(workbook_id)
        if workbook is None or not may_view(caller, workbook):
            raise NotFoundError()
        if not may_edit(caller, workbook):
            raise AuthorizationError("You can view this sheet but not edit it.")
        if payload.version != workbook.version and not payload.force:
            names = await self._workbooks.names([workbook.updated_by_id])
            who = names.get(workbook.updated_by_id) if workbook.updated_by_id else None
            raise ConflictError(f"{who or 'Someone'} saved this sheet after you opened it.")
        workbook.snapshot = scrub_links(payload.snapshot)
        workbook.version += 1
        workbook.updated_by_id = caller.employee_id
        workbook.updated_at = datetime.now(UTC)
        await self._workbooks.flush()
        names = await self._workbooks.names([caller.employee_id])
        return WorkbookSaved(
            version=workbook.version,
            updated_at=workbook.updated_at,
            updated_by_name=names.get(caller.employee_id),
        )

    async def delete(self, caller: CurrentUser, workbook_id: uuid.UUID) -> None:
        workbook = await self._managed(caller, workbook_id)
        await self._log(caller, "sheet.delete", workbook)
        await self._workbooks.delete(workbook)

    # ---------------------------------------------------------------- helpers

    async def _visible(self, caller: CurrentUser, workbook_id: uuid.UUID) -> Workbook:
        workbook = await self._workbooks.get(workbook_id)
        if workbook is None or not may_view(caller, workbook):
            raise NotFoundError()
        return workbook

    async def _managed(self, caller: CurrentUser, workbook_id: uuid.UUID) -> Workbook:
        workbook = await self._visible(caller, workbook_id)
        if not may_manage(caller, workbook):
            raise AuthorizationError("Only the sheet's owner or an admin can do that.")
        return workbook

    async def _read(self, caller: CurrentUser, workbook: Workbook) -> WorkbookRead:
        names = await self._workbooks.names([workbook.owner_id, workbook.updated_by_id])
        summary = self._summary(caller, workbook, names)
        return WorkbookRead(**summary.model_dump(), snapshot=workbook.snapshot or {})

    @staticmethod
    def _summary(
        caller: CurrentUser, workbook: Workbook, names: dict[uuid.UUID, str]
    ) -> WorkbookSummary:
        return WorkbookSummary(
            id=workbook.id,
            title=workbook.title,
            sharing=workbook.sharing,
            owner_id=workbook.owner_id,
            owner_name=names.get(workbook.owner_id),
            version=workbook.version,
            updated_at=workbook.updated_at,
            updated_by_name=names.get(workbook.updated_by_id) if workbook.updated_by_id else None,
            can_edit=may_edit(caller, workbook),
            can_manage=may_manage(caller, workbook),
        )

    async def _log(self, caller: CurrentUser, action: str, workbook: Workbook) -> None:
        await self._audit.append(
            actor=str(caller.employee_id), action=action, target=f"sheet:{workbook.id}"
        )

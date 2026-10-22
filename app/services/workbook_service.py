"""Sheets - spreadsheets kept in Avora, shared like Google Sheets.

Access is the general level (`sharing`) plus people added by name
(`WorkbookMember`, viewer or editor). Who may do what (checked here, never in
the UI):
  view   - owner, admins, anyone when shared with everyone, named people.
  edit   - owner, admins, everyone when shared for editing, named editors.
           Viewers (a read-only role) never edit or create.
  share  - add, change or remove named people: anyone who may edit.
  manage - rename, change general access, delete: owner and admins.

A sheet the caller may not view is a 404, so a restricted sheet's existence is
not revealed. Saves are optimistic: a save based on an old version is refused
with a 409 naming who saved in between.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.core.workbook_snapshot import scrub_links
from app.models.employee import Role
from app.models.notification import NotificationKind
from app.models.workbook import Workbook, WorkbookAccess, WorkbookMember, WorkbookSharing
from app.repositories.audit import AuditRepository
from app.repositories.workbook import SheetScope, WorkbookRepository
from app.schemas.auth import CurrentUser
from app.schemas.common import Page, PageParams
from app.schemas.workbook import (
    MyAccess,
    WorkbookContentSave,
    WorkbookCreate,
    WorkbookMemberRead,
    WorkbookPerson,
    WorkbookRead,
    WorkbookSaved,
    WorkbookSummary,
    WorkbookUpdate,
)
from app.services.notification_service import NotificationService


def may_view(caller: CurrentUser, workbook: Workbook, named: WorkbookAccess | None) -> bool:
    return (
        caller.is_admin
        or workbook.owner_id == caller.employee_id
        or workbook.sharing is not WorkbookSharing.PRIVATE
        or named is not None
    )


def may_edit(caller: CurrentUser, workbook: Workbook, named: WorkbookAccess | None) -> bool:
    if caller.role is Role.VIEWER:
        return False
    return (
        may_manage(caller, workbook)
        or workbook.sharing is WorkbookSharing.ORG_EDIT
        or named is WorkbookAccess.EDIT
    )


def may_manage(caller: CurrentUser, workbook: Workbook) -> bool:
    return caller.is_admin or workbook.owner_id == caller.employee_id


def my_access(caller: CurrentUser, workbook: Workbook, named: WorkbookAccess | None) -> MyAccess:
    if workbook.owner_id == caller.employee_id:
        return "owner"
    return "edit" if may_edit(caller, workbook, named) else "view"


class WorkbookService:
    def __init__(
        self,
        workbooks: WorkbookRepository,
        notifications: NotificationService,
        audit: AuditRepository,
    ) -> None:
        self._workbooks = workbooks
        self._notifications = notifications
        self._audit = audit

    # ------------------------------------------------------------------ reads

    async def list_sheets(
        self, caller: CurrentUser, params: PageParams, scope: SheetScope, query: str | None
    ) -> Page[WorkbookSummary]:
        rows, total = await self._workbooks.list_visible(
            caller.employee_id,
            is_admin=caller.is_admin,
            scope=scope,
            query=query,
            offset=params.offset,
            limit=params.limit,
        )
        items = await self._summaries(caller, rows)
        return Page(items=items, page=params.page, size=params.size, total=total)

    async def get(self, caller: CurrentUser, workbook_id: uuid.UUID) -> WorkbookRead:
        workbook, _ = await self._visible(caller, workbook_id)
        (summary,) = await self._summaries(caller, [workbook])
        return WorkbookRead(**summary.model_dump(), snapshot=workbook.snapshot or {})

    async def members(
        self, caller: CurrentUser, workbook_id: uuid.UUID
    ) -> list[WorkbookMemberRead]:
        workbook, _ = await self._visible(caller, workbook_id)
        return [
            WorkbookMemberRead(
                employee_id=person.id,
                name=person.full_name,
                job_title=person.job_title,
                access=member.access,
            )
            for member, person in await self._workbooks.members(workbook.id)
        ]

    async def people(self, caller: CurrentUser, query: str) -> list[WorkbookPerson]:
        """Colleagues to share with. Names and titles only, for anyone signed in:
        the same facts the org chart shows, and no contact details."""
        if caller.role is Role.VIEWER:
            raise AuthorizationError("Your role can view sheets but not share them.")
        query = query.strip()
        if not query:
            return []
        return [
            WorkbookPerson(
                id=p.id, name=p.full_name, job_title=p.job_title, department=p.department
            )
            for p in await self._workbooks.search_people(query)
            if p.id != caller.employee_id
        ]

    # ----------------------------------------------------------------- writes

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
        return await self.get(caller, workbook.id)

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
        return await self.get(caller, workbook.id)

    async def set_member(
        self,
        caller: CurrentUser,
        workbook_id: uuid.UUID,
        employee_id: uuid.UUID,
        access: WorkbookAccess,
    ) -> list[WorkbookMemberRead]:
        """Share with one person, or change what they may do."""
        workbook = await self._shareable(caller, workbook_id)
        if employee_id == workbook.owner_id:
            raise ValidationError("The owner already has full access.")
        person = await self._workbooks.active_employee(employee_id)
        if person is None:
            raise ValidationError("That person isn't an active employee.")
        member = await self._workbooks.member(workbook.id, employee_id)
        is_new = member is None
        if member is None:
            member = WorkbookMember(
                workbook_id=workbook.id, employee_id=employee_id, added_by_id=caller.employee_id
            )
            self._workbooks.add(member)
        member.access = access
        await self._workbooks.flush()
        await self._log(caller, f"sheet.member.{access.value}", workbook, employee_id)
        if is_new:
            await self._tell_shared(caller, workbook, employee_id, access)
        return await self.members(caller, workbook.id)

    async def remove_member(
        self, caller: CurrentUser, workbook_id: uuid.UUID, employee_id: uuid.UUID
    ) -> None:
        workbook = await self._shareable(caller, workbook_id)
        member = await self._workbooks.member(workbook.id, employee_id)
        if member is None:
            raise NotFoundError()
        await self._log(caller, "sheet.member.remove", workbook, employee_id)
        await self._workbooks.delete(member)

    async def save(
        self, caller: CurrentUser, workbook_id: uuid.UUID, payload: WorkbookContentSave
    ) -> WorkbookSaved:
        workbook = await self._workbooks.get_for_update(workbook_id)
        named = await self._named(caller, workbook)
        if workbook is None or not may_view(caller, workbook, named):
            raise NotFoundError()
        if not may_edit(caller, workbook, named):
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

    async def _named(self, caller: CurrentUser, workbook: Workbook | None) -> WorkbookAccess | None:
        if workbook is None:
            return None
        return (await self._workbooks.access_of([workbook.id], caller.employee_id)).get(workbook.id)

    async def _visible(
        self, caller: CurrentUser, workbook_id: uuid.UUID
    ) -> tuple[Workbook, WorkbookAccess | None]:
        workbook = await self._workbooks.get(workbook_id)
        named = await self._named(caller, workbook)
        if workbook is None or not may_view(caller, workbook, named):
            raise NotFoundError()
        return workbook, named

    async def _managed(self, caller: CurrentUser, workbook_id: uuid.UUID) -> Workbook:
        workbook, _ = await self._visible(caller, workbook_id)
        if not may_manage(caller, workbook):
            raise AuthorizationError("Only the sheet's owner or an admin can do that.")
        return workbook

    async def _shareable(self, caller: CurrentUser, workbook_id: uuid.UUID) -> Workbook:
        workbook, named = await self._visible(caller, workbook_id)
        if not may_edit(caller, workbook, named):
            raise AuthorizationError("Only people who can edit this sheet can share it.")
        return workbook

    async def _summaries(
        self, caller: CurrentUser, rows: Sequence[Workbook]
    ) -> list[WorkbookSummary]:
        ids = [r.id for r in rows]
        named = await self._workbooks.access_of(ids, caller.employee_id)
        shared = await self._workbooks.member_names(ids)
        names = await self._workbooks.names(
            [r.owner_id for r in rows] + [r.updated_by_id for r in rows]
        )
        return [
            WorkbookSummary(
                id=r.id,
                title=r.title,
                sharing=r.sharing,
                owner_id=r.owner_id,
                owner_name=names.get(r.owner_id),
                version=r.version,
                updated_at=r.updated_at,
                updated_by_name=names.get(r.updated_by_id) if r.updated_by_id else None,
                my_access=my_access(caller, r, named.get(r.id)),
                can_edit=may_edit(caller, r, named.get(r.id)),
                can_share=may_edit(caller, r, named.get(r.id)),
                can_manage=may_manage(caller, r),
                shared_with=shared.get(r.id, []),
            )
            for r in rows
        ]

    async def _tell_shared(
        self,
        caller: CurrentUser,
        workbook: Workbook,
        employee_id: uuid.UUID,
        access: WorkbookAccess,
    ) -> None:
        sharer = (await self._workbooks.names([caller.employee_id])).get(caller.employee_id)
        verb = "edit" if access is WorkbookAccess.EDIT else "view"
        await self._notifications.notify(
            recipient_id=employee_id,
            kind=NotificationKind.SHEET_SHARED,
            title=f"{sharer or 'Someone'} shared a sheet with you: {workbook.title}"[:160],
            body=f"You can {verb} it.",
            link=f"/dashboard/sheets/{workbook.id}",
            entity_type="sheet",
            entity_id=workbook.id,
            actor_id=caller.employee_id,
        )

    async def _log(
        self,
        caller: CurrentUser,
        action: str,
        workbook: Workbook,
        person: uuid.UUID | None = None,
    ) -> None:
        target = f"sheet:{workbook.id}" + (f":employee:{person}" if person else "")
        await self._audit.append(actor=str(caller.employee_id), action=action, target=target)

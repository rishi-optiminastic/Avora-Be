"""Workspaces: which ones you may open, and who may open this one.

Two rules decide everything here.

**You see only the workspaces you are a member of.** The switcher is built from
`workspace_members`, so a second company is invisible to everyone who was not
explicitly let in - not merely un-clickable, absent.

**Access to a workspace is granted from INSIDE it, by its own admin.** An admin
of workspace A cannot hand out access to workspace B; they would have to be an
admin in B to do that. The alternative - a global super-admin who can open any
workspace - is exactly the thing two separate companies in one deployment must
not have.

The first member of a brand new workspace is therefore granted from the command
line (`scripts/tenancy.py grant`), by whoever runs the deployment. Bootstrapping
access is a deployment act, not an in-app one.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.tenancy import (
    SHARED_SCHEMA,
    InvalidWorkspaceSlug,
    use_schema,
    validate_slug,
)
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.workspace import Workspace, WorkspaceMember
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.workspace import WorkspaceRepository, normalize_email
from app.schemas.auth import CurrentUser
from app.schemas.workspace import (
    EmployeeTransfer,
    TransferResult,
    WorkspaceAccessGrant,
    WorkspaceMemberRead,
    WorkspaceRead,
)


class WorkspaceService:
    def __init__(
        self,
        session: AsyncSession,
        workspaces: WorkspaceRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._session = session
        self._workspaces = workspaces
        self._employees = employees
        self._audit = audit

    async def current_schema(self) -> str:
        """Which schema this request actually landed in.

        Asked of the database rather than read off the header, so it reports
        where the queries are really going. If those two ever disagree, this is
        the one telling the truth.

        `current_schema()` is a Postgres function and the test database is
        SQLite, which has no schemas at all - there, everything is the shared
        one, which is exactly what a single-workspace install looks like.
        """
        if self._session.get_bind().dialect.name != "postgresql":
            return SHARED_SCHEMA
        found = await self._session.execute(text("SELECT current_schema()"))
        row = found.first()
        return str(row[0]) if row and row[0] else SHARED_SCHEMA

    async def current(self) -> Workspace | None:
        schema = await self.current_schema()
        for workspace in await self._workspaces.list_all():
            if workspace.schema_name == schema:
                return workspace
        return None

    async def mine(self, email: str) -> list[WorkspaceRead]:
        """The workspaces this person may open, for the switcher.

        Keyed on the email in their token, not on an employee row: the whole
        point is to list workspaces they are not currently inside.
        """
        here = await self.current_schema()
        rows = await self._workspaces.list_for_email(email)
        return [
            WorkspaceRead(
                id=w.id,
                slug=w.slug,
                name=w.name,
                location=w.location,
                status=w.status,
                is_current=w.schema_name == here,
            )
            for w in rows
        ]

    async def members(self, caller: CurrentUser) -> list[WorkspaceMemberRead]:
        workspace = await self._require_admin_here(caller)
        rows = await self._workspaces.members(workspace.id)
        return [await self._to_member(row) for row in rows]

    async def grant(
        self, caller: CurrentUser, payload: WorkspaceAccessGrant
    ) -> WorkspaceMemberRead:
        """Let one more person open this workspace.

        Does NOT create an employee record for them. They can sign in and see
        the workspace in their switcher; HR still has to add them properly, and
        the member list shows the gap until that happens.
        """
        workspace = await self._require_admin_here(caller)
        me = await self._employees.get(caller.employee_id)
        member = await self._workspaces.grant(
            workspace.id,
            payload.email,
            granted_by_email=me.work_email if me is not None else None,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="workspace.access.grant",
            target=f"workspace:{workspace.slug}:{member.email}",
        )
        return await self._to_member(member)

    async def revoke(self, caller: CurrentUser, email: str) -> None:
        workspace = await self._require_admin_here(caller)
        address = normalize_email(email)
        me = await self._employees.get(caller.employee_id)
        if me is not None and normalize_email(me.work_email) == address:
            # Locking yourself out of the workspace you are administering leaves
            # it with one fewer admin and no way back in from the app.
            raise ConflictError("You cannot remove your own access to this workspace.")
        if not await self._workspaces.is_member(workspace.id, address):
            raise NotFoundError()
        await self._workspaces.revoke(workspace.id, address)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="workspace.access.revoke",
            target=f"workspace:{workspace.slug}:{address}",
        )

    async def transfer(self, caller: CurrentUser, payload: EmployeeTransfer) -> TransferResult:
        """Move one person from this workspace to another.

        **Their history does not travel with them.** Payslips, attendance,
        leave and expenses stay in the workspace that produced them, because
        they belong to the legal entity that paid and employed them - a payslip
        cannot be reassigned to a different company. So this is an offboard
        here and a joiner there, which is exactly what an inter-entity transfer
        is in payroll terms.

        What does travel is who they are: name, work email, date of birth,
        gender, job title and department. Never their role - they arrive as an
        ordinary EXECUTIVE, and privilege is granted inside the new workspace
        by its own admin (the same rule the HR webhook follows).

        Both halves run in ONE transaction across two schemas. If the new row
        cannot be written, the offboard rolls back with it - the alternative is
        somebody deactivated in one workspace and absent from the other.
        """
        source = await self._require_admin_here(caller)
        target = await self._transfer_target(caller, payload.target_slug, source)

        employee = await self._employees.get(payload.employee_id)
        if employee is None or not employee.is_active:
            raise NotFoundError()
        starts_on = payload.starts_on or date.today()
        identity: Mapping[str, object] = {
            "hr_external_id": employee.hr_external_id,
            "work_email": employee.work_email,
            "full_name": employee.full_name,
            "department": employee.department,
            "job_title": employee.job_title,
            "date_of_birth": employee.date_of_birth,
            "gender": employee.gender,
        }
        name, email = employee.full_name, employee.work_email

        # Offboard here first, so the unique work_email in the source workspace
        # is released before anything is written in the target.
        employee.is_active = False
        employee.status = EmployeeStatus.INACTIVE
        await self._session.flush()

        new_id = await self._place_in_target(target, identity, starts_on, email, caller)

        # They have left this workspace, so their grant to open it goes with
        # them. Without this it lingers: the workspace keeps appearing in their
        # switcher, and picking it gives a baffling 401 (the employee row here
        # is inactive) rather than saying they no longer work there.
        await self._workspaces.revoke(source.id, email)

        await self._audit.append(
            actor=str(caller.employee_id),
            action="workspace.transfer.out",
            target=f"employee:{payload.employee_id}:to:{target.slug}",
        )
        return TransferResult(
            employee_id=payload.employee_id,
            employee_name=name,
            from_slug=source.slug,
            to_slug=target.slug,
            new_employee_id=new_id,
            starts_on=starts_on,
        )

    async def _transfer_target(
        self, caller: CurrentUser, slug: str, source: Workspace
    ) -> Workspace:
        """The workspace someone may be sent to.

        The caller has to be able to open it themselves. Without that, an admin
        of one company could inject people into another company's roster from
        outside it, which is the exact thing separate workspaces exist to stop.
        """
        try:
            key = validate_slug(slug)
        except InvalidWorkspaceSlug as exc:
            # A client-supplied key, so a bad one is a 422 - not the 500 a raw
            # ValueError would escape as.
            raise ValidationError(str(exc)) from exc
        target = await self._workspaces.get_by_slug(key)
        if target is None or target.provisioned_at is None:
            raise ValidationError("That workspace is not available.")
        if target.id == source.id:
            raise ValidationError("They are already in this workspace.")
        me = await self._employees.get(caller.employee_id)
        email = me.work_email if me is not None else ""
        if not await self._workspaces.is_member(target.id, email):
            raise AuthorizationError("You do not have access to that workspace.")
        return target

    async def _place_in_target(
        self,
        target: Workspace,
        identity: Mapping[str, object],
        starts_on: date,
        email: str,
        caller: CurrentUser,
    ) -> uuid.UUID:
        """Create the joiner inside the target workspace's schema.

        The session is pointed at the other schema for the length of this, then
        put back. Still one transaction: a failure here rolls the offboard back
        too. `expunge_all` first because the identity map is full of rows from
        THIS workspace, and a lookup after the switch would otherwise be
        answered from the wrong schema's cache.
        """
        here = await self.current_schema()
        self._session.expunge_all()
        await use_schema(self._session, target.schema_name)
        try:
            employees = EmployeeRepository(self._session)
            existing = await employees.get_by_work_email(email)
            if existing is not None and existing.is_active:
                raise ConflictError("They already work in that workspace.")
            placed = Employee(
                **identity,
                hire_date=starts_on,
                # Never carries privilege across: they arrive ordinary, and the
                # target workspace's own admin decides what they may do.
                role=Role.EXECUTIVE,
                status=EmployeeStatus.ACTIVE,
                is_active=True,
            )
            self._session.add(placed)
            try:
                await self._session.flush()
            except IntegrityError as exc:
                # They already exist there under a different row - the same HR
                # id or work email. A readable 409 beats a 500, and raising
                # here rolls the whole transfer back, offboard included.
                raise ConflictError("They already have a record in that workspace.") from exc
            await self._workspaces.grant(target.id, email, granted_by_email=None)
            await self._audit.append(
                actor=str(caller.employee_id),
                action="workspace.transfer.in",
                target=f"employee:{placed.id}:from:{here}",
            )
            return placed.id
        finally:
            # Always back, even on failure: the request continues on this
            # session and must not keep writing into someone else's schema.
            self._session.expunge_all()
            await use_schema(self._session, here)

    async def _require_admin_here(self, caller: CurrentUser) -> Workspace:
        """Admin of the workspace this request is in - not of any other one."""
        if not caller.is_admin:
            raise AuthorizationError("Only an admin can manage workspace access.")
        workspace = await self.current()
        if workspace is None:
            raise ConflictError(
                "This workspace is not registered yet. Run `scripts/tenancy.py bootstrap`."
            )
        return workspace

    async def _to_member(self, member: WorkspaceMember) -> WorkspaceMemberRead:
        """Membership plus whether they actually have an account HERE.

        The employee lookup runs in the current workspace's schema, which is the
        point: the same email is a full employee in one workspace and a blank
        row in another, and the member list should show that.
        """
        employee = await self._employees.get_by_work_email(member.email)
        return WorkspaceMemberRead(
            email=member.email,
            employee_name=employee.full_name if employee is not None else None,
            has_account=employee is not None and employee.is_active,
            granted_by_email=member.granted_by_email,
            granted_at=member.created_at,
        )

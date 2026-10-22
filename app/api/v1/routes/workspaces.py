"""Workspace endpoints - the switcher, and who may open this workspace.

A workspace is CREATED from the command line, never from here
(`scripts/tenancy.py create`): building one runs the whole migration chain
against a new schema, and standing up a second company is a deployment act.

What the API does is the day-to-day part: show someone the workspaces they may
open, and let that workspace's own admin decide who else may.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.core.deps import AuthIdentityDep, CurrentUserDep, WorkspaceServiceDep
from app.schemas.workspace import (
    EmployeeTransfer,
    TransferResult,
    WorkspaceAccessGrant,
    WorkspaceMemberRead,
    WorkspaceRead,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("/mine", response_model=list[WorkspaceRead])
async def my_workspaces(
    identity: AuthIdentityDep, service: WorkspaceServiceDep
) -> list[WorkspaceRead]:
    """The workspaces this person may open, for the switcher.

    Keyed on the email in their token rather than an employee record, because
    the whole point is to list workspaces they are not currently inside. A
    workspace they were never granted does not appear at all.
    """
    return await service.mine(identity.email)


@router.get("/members", response_model=list[WorkspaceMemberRead])
async def workspace_members(
    caller: CurrentUserDep, service: WorkspaceServiceDep
) -> list[WorkspaceMemberRead]:
    """Who may open THIS workspace. Admin of this workspace only."""
    return await service.members(caller)


@router.post("/members", response_model=WorkspaceMemberRead, status_code=status.HTTP_201_CREATED)
async def grant_workspace_access(
    payload: WorkspaceAccessGrant, caller: CurrentUserDep, service: WorkspaceServiceDep
) -> WorkspaceMemberRead:
    """Let one more person open this workspace.

    Does not create an employee record for them - HR still adds them properly.
    The member list shows that gap until it is closed.
    """
    return await service.grant(caller, payload)


@router.delete("/members/{email}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_workspace_access(
    email: str, caller: CurrentUserDep, service: WorkspaceServiceDep
) -> None:
    """Take someone's access to this workspace away. Their employee record and
    everything they did stays; they simply can no longer open it."""
    await service.revoke(caller, email)


@router.post("/transfer", response_model=TransferResult)
async def transfer_employee(
    payload: EmployeeTransfer, caller: CurrentUserDep, service: WorkspaceServiceDep
) -> TransferResult:
    """Move one person from this workspace to another.

    An offboard here and a joiner there: their payslips, attendance and leave
    stay with the entity that produced them, because that is who employed and
    paid them. Their identity travels; their role does not - they arrive as an
    ordinary employee and the target workspace's own admin decides the rest.

    Admin of THIS workspace, and a member of the target. 409 when they already
    work there, 422 when the target is unavailable or is this one.
    """
    return await service.transfer(caller, payload)

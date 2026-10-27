"""Workspace schemas - the switcher's list, and who may enter one."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, EmailStr, Field

from app.models.workspace import WorkspaceStatus


class WorkspaceRead(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    location: str
    status: WorkspaceStatus
    # True for the workspace this request is already looking at.
    is_current: bool = False


class WorkspaceMemberRead(BaseModel):
    """One person allowed into this workspace.

    `email` is the key, not an employee id: access is granted before the person
    has an employee record here, which is how someone gets into a second
    workspace at all. `employee_name` is filled in when they do have one.
    """

    email: str
    employee_name: str | None = None
    # False when they can open the workspace but have no employee record in it
    # yet - visible on purpose, because that is a half-finished setup.
    has_account: bool = False
    granted_by_email: str | None = None
    granted_at: datetime


class WorkspaceAccessGrant(BaseModel):
    email: EmailStr = Field(max_length=320)


class GuestRole(StrEnum):
    """What someone from outside may hold in a workspace that does not employ them.

    A deliberate subset of `Role`, not the whole enum. Every role left out
    takes its scope from a position in THIS workspace's org chart - a reporting
    line (`manager`, `executive`, `intern`), a department (`senior_manager`),
    or an explicit grant (`viewer`, whose scope resolves to "only yourself").
    A guest occupies none of those, so any of them would hand somebody an
    account that can see nothing but their own empty record. `hr` and `admin`
    are the two whose scope is the workspace itself, which is the only kind of
    access that means anything to a visitor.

    Values match `Role` exactly, so `Role(value)` converts without a mapping.
    """

    HR = "hr"
    ADMIN = "admin"


class WorkspaceGuestInvite(BaseModel):
    """Give somebody an account here without employing them.

    The role is chosen by THIS workspace's admin rather than carried over from
    wherever the person comes from: roles are per-workspace, and nothing in the
    shared address book records what anyone holds elsewhere. Granting privilege
    inside the workspace that bears the consequences is the same rule the HR
    webhook follows (section 5.5) and the one `transfer` follows.
    """

    email: EmailStr = Field(max_length=320)
    # Backfilled from the identity provider on first sign-in
    # (`refresh_display_name`), so leaving it blank costs nothing permanent.
    name: str = Field(default="", max_length=120)
    role: GuestRole


class EmployeeTransfer(BaseModel):
    """Move one person from the workspace you are in to another one."""

    employee_id: uuid.UUID
    # The workspace they are going to, by its key.
    target_slug: str = Field(min_length=2, max_length=40)
    # Their first day at the new entity. Defaults to today, and becomes their
    # hire_date there - which is what drives probation, leave accrual and
    # tenure in the new workspace.
    starts_on: date | None = None


class TransferResult(BaseModel):
    """What the transfer did, in both workspaces."""

    employee_id: uuid.UUID
    employee_name: str
    from_slug: str
    to_slug: str
    # Their id in the NEW workspace. A different row in a different schema -
    # the same person, not the same record.
    new_employee_id: uuid.UUID
    starts_on: date

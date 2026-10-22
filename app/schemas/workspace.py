"""Workspace schemas - the switcher's list, and who may enter one."""

from __future__ import annotations

import uuid
from datetime import date, datetime

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

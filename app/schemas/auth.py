"""The authenticated principal — derived server-side, never from the client."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.models.employee import Role


class CurrentUser(BaseModel):
    """A human caller. role/team are re-derived from the DB, not the token."""

    employee_id: uuid.UUID
    role: Role
    manager_id: uuid.UUID | None

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    @property
    def is_manager(self) -> bool:
        return self.role in (Role.MANAGER, Role.SENIOR_MANAGER, Role.ADMIN, Role.HR)


class AuthIdentity(BaseModel):
    """A verified Better Auth identity that may not yet be an employee.

    Used by flows that authenticate a person before they have an employee
    record — e.g. accepting an invitation, which then provisions one.
    """

    subject: str
    email: str
    # The display name the provider asserts (Google profile / sign-up name). Used
    # to provision and keep an employee's `full_name` real instead of an
    # email-derived placeholder. None when the token carries no name.
    name: str | None = None


class CurrentDevice(BaseModel):
    """An authenticated agent device."""

    device_id: uuid.UUID
    employee_id: uuid.UUID
    last_sequence: int


class EnvPrincipal(BaseModel):
    """An Env Sync caller — either a human (Better Auth JWT) or the VSCode
    extension presenting a Personal Access Token. Resolved to an employee id
    server-side; membership/role is re-derived per project, never trusted."""

    employee_id: uuid.UUID

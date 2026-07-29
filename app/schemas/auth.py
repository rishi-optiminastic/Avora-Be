"""The authenticated principal — derived server-side, never from the client."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, field_validator

from app.models.employee import Role


class CurrentUser(BaseModel):
    """A human caller. role/team are re-derived from the DB, not the token."""

    employee_id: uuid.UUID
    role: Role
    manager_id: uuid.UUID | None

    @field_validator("role")
    @classmethod
    def _normalize_role(cls, role: Role) -> Role:
        # IT admins are treated as full admins for authorization (org decision):
        # collapse the role here so every downstream check sees ADMIN. The stored
        # employee role stays `it_admin` for display; only effective access changes.
        return Role.ADMIN if role is Role.IT_ADMIN else role

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

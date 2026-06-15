"""Invitation request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.employee import Role
from app.models.invitation import InvitationStatus
from app.schemas.common import ORMModel


class InvitationCreate(BaseModel):
    """Admin payload to invite someone at a given role."""

    email: EmailStr
    role: Role
    department: str | None = Field(default=None, max_length=128)


class InvitationRead(ORMModel):
    """Admin-facing view of an invitation (no token)."""

    id: uuid.UUID
    email: str
    role: Role
    department: str | None
    status: InvitationStatus
    expires_at: datetime


class InvitationCreated(BaseModel):
    """Returned on create — includes the accept URL so the admin can copy it."""

    id: uuid.UUID
    email: str
    role: Role
    department: str | None
    accept_url: str
    expires_at: datetime


class InvitationInfo(BaseModel):
    """Public, token-gated view shown on the acceptance page."""

    email: str
    role: Role
    department: str | None
    org_name: str
    inviter_name: str | None
    expires_at: datetime


class InvitationAccept(BaseModel):
    token: str

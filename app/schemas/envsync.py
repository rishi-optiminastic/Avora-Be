"""Env Sync request/response schemas.

The wire shapes intentionally mirror the original `envsync` Django serializers so
the existing VSCode extension talks to this backend unchanged: `version_id`,
`hash`, `content`, `updated_by`, `updated_at`, and the 409 `{detail, head}` body.
The ORM model is never returned directly (Golden rule #5).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.env_project import EnvMemberRole

# Env files are small; cap to keep a copied token from being used to bloat storage.
_MAX_ENV_CONTENT = 1_000_000


class EnvProjectRead(BaseModel):
    id: uuid.UUID
    name: str
    role: EnvMemberRole
    department: str | None = None


class EnvProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    department: str | None = Field(default=None, max_length=128)


class CollaboratorRead(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    role: EnvMemberRole


class CollaboratorAdd(BaseModel):
    # A work email — the real check is the employee lookup in the service, so we
    # only constrain length/shape here (EmailStr rejects reserved test domains).
    email: str = Field(min_length=3, max_length=320)
    role: EnvMemberRole = EnvMemberRole.EDITOR


class EnvironmentRead(BaseModel):
    environment: str
    version_id: uuid.UUID
    hash: str
    updated_by: str
    updated_at: datetime


class EnvVersionRead(BaseModel):
    version_id: uuid.UUID
    hash: str
    content: str
    updated_by: str
    updated_at: datetime


class EnvHistoryRead(BaseModel):
    version_id: uuid.UUID
    hash: str
    updated_by: str
    updated_at: datetime


class EnvPush(BaseModel):
    # allow blank — an empty env is a legitimate state.
    content: str = Field(max_length=_MAX_ENV_CONTENT)
    base_version: uuid.UUID | None = None
    environment: str = Field(default="default", min_length=1, max_length=50)


class TokenCreate(BaseModel):
    label: str = Field(min_length=1, max_length=256)


class TokenRead(BaseModel):
    id: uuid.UUID
    label: str
    created_at: datetime
    last_used_at: datetime | None = None
    is_revoked: bool


class TokenCreated(TokenRead):
    # The raw token — returned ONCE at creation, never stored or shown again.
    token: str

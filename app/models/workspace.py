"""The workspace list and who may enter each one.

These two tables are the ONLY ones that live outside a workspace, in `public`.
They have to: answering "which workspaces may this email enter" is what decides
which schema a request reads, so it cannot itself live inside one.

Everything else - employees, payroll, leave, screenshots - lives in the
workspace's own schema and is reached through `search_path` (see
`app/core/tenancy.py`). There is deliberately no `workspace_id` column on those
tables; the schema IS the boundary.

`WorkspaceMember` is access, not identity. It says "this email address may open
this workspace". What they can DO once inside is their `employees.role` row in
that workspace, which is where it has always been.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

_SHARED = {"schema": "public"}


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    # Readable but closed to everyone except the people who run Avora. Used
    # while a workspace is being set up, and instead of deleting one.
    SUSPENDED = "suspended"


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_workspaces_slug"),
        _SHARED,
    )

    # Short key used in the `X-Avora-Workspace` header and the URL. Immutable
    # once created; validated hard - see `tenancy.validate_slug`.
    slug: Mapped[str] = mapped_column(String(40), index=True)
    # The Postgres schema holding this workspace's 69 tables. Recorded rather
    # than derived from the slug because the FIRST workspace keeps `public`,
    # where its data already is - moving live payroll and attendance tables
    # would be the most dangerous step in this change, for no gain.
    schema_name: Mapped[str] = mapped_column(String(63), default="public")
    name: Mapped[str] = mapped_column(String(120))
    # Short qualifier, shown as "name · location" in the switcher.
    location: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[WorkspaceStatus] = mapped_column(default=WorkspaceStatus.ACTIVE, index=True)

    # Set once the workspace's tables have been built by the migration runner.
    # Until then it exists but nobody can be let into it.
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # No FK: the creator is an employee inside a workspace, and this table sits
    # outside every workspace. Stored for the audit trail, read by nothing.
    created_by_email: Mapped[str | None] = mapped_column(String(320), default=None)


class WorkspaceMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One email address allowed into one workspace.

    Keyed on EMAIL, not an employee id, for two reasons. The employee row lives
    inside the workspace's schema, so a foreign key could not reach it. And
    access has to be grantable BEFORE the person exists there - that is how
    somebody gets invited into a second workspace at all.
    """

    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "email", name="uq_workspace_members_workspace_email"),
        Index("ix_workspace_members_email", "email"),
        _SHARED,
    )

    # No FK to workspaces on purpose: this table is written by an admin service
    # that already checks the workspace exists, and a cascade here would delete
    # the record of who had access when a workspace is suspended.
    workspace_id: Mapped[uuid.UUID] = mapped_column(index=True)
    # Always stored lowercased - the caller's email comes from an OIDC token and
    # its casing is not stable.
    email: Mapped[str] = mapped_column(String(320))
    granted_by_email: Mapped[str | None] = mapped_column(String(320), default=None)

"""Env Sync — Git-style pull/push/sync of project `.env` files (the first
"Avora App Store" app, for the engineering/tech department).

A project groups collaborators around one set of secrets. Each push creates an
immutable, **encrypted** `EnvVersion`; the latest row for a (project,
environment) is the current "head". History is the parent-version chain. Secrets
never hit the DB as plaintext (Security rule 5.6) — see `core/envsync_crypto.py`.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EnvMemberRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


# Roles allowed to push (write) a new version — viewers are read-only.
_WRITER_ROLES = frozenset({EnvMemberRole.OWNER, EnvMemberRole.EDITOR})


class EnvProject(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "env_projects"

    name: Mapped[str] = mapped_column(String(200))
    # Who created it (always also seeded as an OWNER member). SET NULL so
    # soft-deleting the owner never orphans the project's secrets.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    # Optional department tag — drives App Store grouping (matches Employee.department).
    department: Mapped[str | None] = mapped_column(String(128), default=None, index=True)


class EnvProjectMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "env_project_members"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "employee_id", name="uq_env_project_members_project_employee"
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("env_projects.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[EnvMemberRole] = mapped_column(default=EnvMemberRole.EDITOR)

    @property
    def can_write(self) -> bool:
        return self.role in _WRITER_ROLES


class EnvVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable, encrypted snapshot of a (project, environment) env file."""

    __tablename__ = "env_versions"
    __table_args__ = (
        Index("ix_env_versions_project_env_created", "project_id", "environment", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("env_projects.id", ondelete="CASCADE"), index=True
    )
    environment: Mapped[str] = mapped_column(String(50), default="default")
    # Fernet ciphertext — the raw column never holds plaintext secrets.
    content_encrypted: Mapped[str] = mapped_column(Text)
    # SHA-256 of the PLAINTEXT — must match the extension's hashContent() so both
    # sides agree on "are these identical" without decrypting on the client.
    content_hash: Mapped[str] = mapped_column(String(64))
    # History chain. SET NULL so pruning a parent never deletes its descendants.
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("env_versions.id", ondelete="SET NULL"), default=None
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )

"""Workspace + membership data access, against the SHARED `public` schema.

Every method here is deliberately schema-qualified by the models themselves
(`__table_args__ = {"schema": "public"}`), so these reads work whatever
`search_path` the session is currently pointed at. That matters: membership is
looked up BEFORE a workspace is chosen, and again from inside one when the
switcher renders.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import Workspace, WorkspaceMember, WorkspaceStatus


def normalize_email(email: str) -> str:
    """Membership is keyed on email, and OIDC casing is not stable."""
    return email.strip().lower()


class WorkspaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, workspace_id: uuid.UUID) -> Workspace | None:
        return await self._session.get(Workspace, workspace_id)

    async def get_by_slug(self, slug: str) -> Workspace | None:
        found: Workspace | None = await self._session.scalar(
            select(Workspace).where(Workspace.slug == slug)
        )
        return found

    async def list_all(self) -> Sequence[Workspace]:
        rows = await self._session.scalars(select(Workspace).order_by(Workspace.name))
        return list(rows.all())

    async def list_for_email(self, email: str) -> Sequence[Workspace]:
        """The workspaces this person may open.

        Suspended ones are excluded: the row stays so access is not forgotten,
        but nobody is let in while it is closed.
        """
        rows = await self._session.scalars(
            select(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(
                WorkspaceMember.email == normalize_email(email),
                Workspace.status == WorkspaceStatus.ACTIVE,
                Workspace.provisioned_at.is_not(None),
            )
            .order_by(Workspace.name)
        )
        return list(rows.all())

    async def is_member(self, workspace_id: uuid.UUID, email: str) -> bool:
        found = await self._session.scalar(
            select(WorkspaceMember.id).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.email == normalize_email(email),
            )
        )
        return found is not None

    async def members(self, workspace_id: uuid.UUID) -> Sequence[WorkspaceMember]:
        rows = await self._session.scalars(
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace_id)
            .order_by(WorkspaceMember.email)
        )
        return list(rows.all())

    def add(self, workspace: Workspace) -> None:
        self._session.add(workspace)

    async def grant(
        self, workspace_id: uuid.UUID, email: str, granted_by_email: str | None
    ) -> WorkspaceMember:
        """Let this email into this workspace. Idempotent - granting twice is
        the same as granting once, so a double-click is harmless."""
        address = normalize_email(email)
        existing = await self._session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.email == address,
            )
        )
        if existing is not None:
            return existing
        member = WorkspaceMember(
            workspace_id=workspace_id,
            email=address,
            granted_by_email=granted_by_email,
        )
        self._session.add(member)
        await self._session.flush()
        return member

    async def revoke(self, workspace_id: uuid.UUID, email: str) -> None:
        await self._session.execute(
            delete(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.email == normalize_email(email),
            )
        )

    async def member_count(self, workspace_id: uuid.UUID) -> int:
        rows = await self._session.scalars(
            select(WorkspaceMember.id).where(WorkspaceMember.workspace_id == workspace_id)
        )
        return len(list(rows.all()))

    async def flush(self) -> None:
        await self._session.flush()

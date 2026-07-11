"""Changelog business rules.

Every authenticated employee may read the changelog; only an admin may publish,
edit, or remove an entry. No FastAPI objects here (route → service → repo).
"""

from __future__ import annotations

import uuid

from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.changelog import ChangelogEntry
from app.repositories.audit import AuditRepository
from app.repositories.changelog import ChangelogRepository
from app.schemas.auth import CurrentUser
from app.schemas.changelog import ChangelogCreate, ChangelogUpdate


class ChangelogService:
    def __init__(self, entries: ChangelogRepository, audit: AuditRepository) -> None:
        self._entries = entries
        self._audit = audit

    async def list(self, *, limit: int, offset: int) -> tuple[list[ChangelogEntry], int]:
        rows = list(await self._entries.list_recent(limit=limit, offset=offset))
        total = await self._entries.count()
        return rows, total

    async def create(self, caller: CurrentUser, payload: ChangelogCreate) -> ChangelogEntry:
        if not caller.is_admin:
            raise AuthorizationError()
        row = await self._entries.create(
            title=payload.title,
            body=payload.body,
            category=payload.category.value,
            version=payload.version,
            created_by=caller.employee_id,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="changelog.create",
            target=f"changelog:{row.id}",
        )
        return row

    async def update(
        self, caller: CurrentUser, entry_id: uuid.UUID, payload: ChangelogUpdate
    ) -> ChangelogEntry:
        if not caller.is_admin:
            raise AuthorizationError()
        row = await self._entries.get(entry_id)
        if row is None:
            raise NotFoundError()
        fields = payload.model_dump(exclude_unset=True)
        if "title" in fields:
            row.title = fields["title"]
        if "body" in fields:
            row.body = fields["body"]
        if "category" in fields and fields["category"] is not None:
            row.category = str(fields["category"])
        if "version" in fields:
            row.version = fields["version"]
        await self._entries.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="changelog.update",
            target=f"changelog:{entry_id}",
        )
        return row

    async def delete(self, caller: CurrentUser, entry_id: uuid.UUID) -> None:
        if not caller.is_admin:
            raise AuthorizationError()
        row = await self._entries.get(entry_id)
        if row is None:
            raise NotFoundError()
        await self._entries.delete(row)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="changelog.delete",
            target=f"changelog:{entry_id}",
        )

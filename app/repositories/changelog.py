"""Changelog data access. Visibility (everyone reads, admin writes) is enforced
by the service; this layer only builds queries."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.changelog import ChangelogEntry

# Server-side cap so the public list can never be asked for an unbounded page.
MAX_PAGE = 100


class ChangelogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        title: str,
        body: str,
        category: str,
        version: str | None,
        created_by: uuid.UUID | None,
    ) -> ChangelogEntry:
        row = ChangelogEntry(
            title=title,
            body=body,
            category=category,
            version=version,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, entry_id: uuid.UUID) -> ChangelogEntry | None:
        return await self._session.get(ChangelogEntry, entry_id)

    async def list_recent(self, *, limit: int, offset: int) -> Sequence[ChangelogEntry]:
        """Newest first — the timeline order for the What's-new page."""
        capped = max(1, min(limit, MAX_PAGE))
        rows = await self._session.execute(
            select(ChangelogEntry)
            .order_by(ChangelogEntry.created_at.desc())
            .limit(capped)
            .offset(max(0, offset))
        )
        return rows.scalars().all()

    async def count(self) -> int:
        result = await self._session.execute(select(func.count()).select_from(ChangelogEntry))
        return int(result.scalar_one())

    async def delete(self, entry: ChangelogEntry) -> None:
        await self._session.delete(entry)
        await self._session.flush()

    async def flush(self) -> None:
        await self._session.flush()

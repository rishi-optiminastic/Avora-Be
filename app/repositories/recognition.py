"""Recognition data access — the feed, one award, and its comment thread."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recognition import Recognition, RecognitionBadge, RecognitionComment

MAX_PAGE = 100


class RecognitionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        employee_id: uuid.UUID,
        awarded_by_id: uuid.UUID,
        badge: RecognitionBadge,
        note: str,
    ) -> Recognition:
        row = Recognition(
            employee_id=employee_id,
            awarded_by_id=awarded_by_id,
            badge=badge,
            note=note,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, recognition_id: uuid.UUID) -> Recognition | None:
        return await self._session.get(Recognition, recognition_id)

    async def feed(
        self,
        *,
        employee_id: uuid.UUID | None = None,
        badge: RecognitionBadge | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Recognition]:
        """Newest first. The feed is org-wide on purpose - recognition nobody
        else can see is a private message, not a programme."""
        stmt = select(Recognition)
        if employee_id is not None:
            stmt = stmt.where(Recognition.employee_id == employee_id)
        if badge is not None:
            stmt = stmt.where(Recognition.badge == badge)
        stmt = (
            stmt.order_by(Recognition.created_at.desc())
            .limit(min(limit, MAX_PAGE))
            .offset(max(0, offset))
        )
        rows = await self._session.execute(stmt)
        return rows.scalars().all()

    async def delete(self, row: Recognition) -> None:
        await self._session.delete(row)
        await self._session.flush()

    # --- comments ----------------------------------------------------------- #

    async def add_comment(
        self, *, recognition_id: uuid.UUID, author_id: uuid.UUID, body: str
    ) -> RecognitionComment:
        comment = RecognitionComment(recognition_id=recognition_id, author_id=author_id, body=body)
        self._session.add(comment)
        await self._session.flush()
        return comment

    async def get_comment(self, comment_id: uuid.UUID) -> RecognitionComment | None:
        return await self._session.get(RecognitionComment, comment_id)

    async def comments_for(
        self, recognition_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[RecognitionComment]]:
        """Every thread for a page of the feed in one query, oldest first, so
        rendering the feed is two round trips rather than one per card."""
        if not recognition_ids:
            return {}
        rows = await self._session.execute(
            select(RecognitionComment)
            .where(RecognitionComment.recognition_id.in_(recognition_ids))
            .order_by(RecognitionComment.created_at.asc())
        )
        out: dict[uuid.UUID, list[RecognitionComment]] = {}
        for comment in rows.scalars().all():
            out.setdefault(comment.recognition_id, []).append(comment)
        return out

    async def delete_comment(self, comment: RecognitionComment) -> None:
        await self._session.delete(comment)
        await self._session.flush()

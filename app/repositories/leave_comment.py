"""Leave comment data access. Authorization is done via the leave's scope by the
service before these are ever called."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.leave_comment import LeaveComment


class LeaveCommentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, leave_id: uuid.UUID, author_id: uuid.UUID, body: str) -> LeaveComment:
        comment = LeaveComment(leave_id=leave_id, author_id=author_id, body=body)
        self._session.add(comment)
        await self._session.flush()
        return comment

    async def list_for_leave(self, leave_id: uuid.UUID) -> Sequence[LeaveComment]:
        rows = await self._session.execute(
            select(LeaveComment)
            .where(LeaveComment.leave_id == leave_id)
            .order_by(LeaveComment.created_at.asc())
        )
        return rows.scalars().all()

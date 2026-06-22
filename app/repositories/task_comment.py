"""Task comment data access. Authorization is done via the task's scope by the
service before these are ever called (mirrors LeaveCommentRepository)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task_comment import TaskComment


class TaskCommentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        task_id: uuid.UUID,
        author_id: uuid.UUID,
        body: str,
        idempotency_key: str | None = None,
    ) -> TaskComment:
        comment = TaskComment(
            task_id=task_id, author_id=author_id, body=body, idempotency_key=idempotency_key
        )
        self._session.add(comment)
        await self._session.flush()
        return comment

    async def insert_unique(
        self,
        *,
        task_id: uuid.UUID,
        author_id: uuid.UUID,
        body: str,
        idempotency_key: str | None,
    ) -> TaskComment:
        """Insert a comment, tolerating a concurrent insert under the same key.

        The pre-check in the service catches the common (sequential) replay; this
        closes the narrow window where two requests with the same key race past it
        at once. The DB unique constraint rejects the loser, and we return the row
        the winner already committed. A savepoint keeps the outer transaction usable
        after the caught IntegrityError.
        """
        if idempotency_key is None:
            # No constraint to race on (NULL keys are distinct); the keyless content
            # window in the service already guarded the duplicate case.
            return await self.create(task_id=task_id, author_id=author_id, body=body)
        try:
            async with self._session.begin_nested():
                comment = TaskComment(
                    task_id=task_id,
                    author_id=author_id,
                    body=body,
                    idempotency_key=idempotency_key,
                )
                self._session.add(comment)
                await self._session.flush()
            return comment
        except IntegrityError:
            winner = await self.find_by_idempotency_key(
                author_id=author_id, idempotency_key=idempotency_key
            )
            if winner is None:
                raise
            return winner

    async def find_by_idempotency_key(
        self, *, author_id: uuid.UUID, idempotency_key: str
    ) -> TaskComment | None:
        """The author's earlier comment posted under this idempotency key, if any.

        Scoped to the author so one user's key can never resolve another user's row.
        """
        row = await self._session.execute(
            select(TaskComment).where(
                TaskComment.author_id == author_id,
                TaskComment.idempotency_key == idempotency_key,
            )
        )
        return row.scalars().first()

    async def find_recent_duplicate(
        self, *, task_id: uuid.UUID, author_id: uuid.UUID, body: str, since: datetime
    ) -> TaskComment | None:
        """The same author's identical comment on this task since `since`, if any.

        Used to make comment creation idempotent: a copied/replayed request (or a
        double-click) returns the existing row instead of inserting a duplicate.
        """
        row = await self._session.execute(
            select(TaskComment)
            .where(
                TaskComment.task_id == task_id,
                TaskComment.author_id == author_id,
                TaskComment.body == body,
                TaskComment.created_at >= since,
            )
            .order_by(TaskComment.created_at.desc())
            .limit(1)
        )
        return row.scalars().first()

    async def list_for_task(self, task_id: uuid.UUID) -> Sequence[TaskComment]:
        rows = await self._session.execute(
            select(TaskComment)
            .where(TaskComment.task_id == task_id)
            .order_by(TaskComment.created_at.asc())
        )
        return rows.scalars().all()

    async def count_for_task(self, task_id: uuid.UUID) -> int:
        total = await self._session.scalar(
            select(func.count()).select_from(TaskComment).where(TaskComment.task_id == task_id)
        )
        return int(total or 0)

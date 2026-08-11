"""Notification data access — the only place inbox queries are built.

Scope is simple and absolute: a caller only ever reads or mutates rows whose
`recipient_id` is their own employee id. There is no manager-can-read-reports
widening here — an inbox is private to its owner.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification, NotificationKind, NotificationLevel


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        recipient_id: uuid.UUID,
        kind: NotificationKind,
        level: NotificationLevel,
        title: str,
        body: str | None,
        link: str | None,
        entity_type: str | None,
        entity_id: uuid.UUID | None,
        actor_id: uuid.UUID | None,
    ) -> Notification:
        row = Notification(
            recipient_id=recipient_id,
            kind=kind,
            level=level,
            title=title,
            body=body,
            link=link,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_id=actor_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def create_isolated(
        self,
        *,
        recipient_id: uuid.UUID,
        kind: NotificationKind,
        level: NotificationLevel,
        title: str,
        body: str | None,
        link: str | None,
        entity_type: str | None,
        entity_id: uuid.UUID | None,
        actor_id: uuid.UUID | None,
    ) -> Notification | None:
        """`create`, but inside a SAVEPOINT that absorbs a DB-level failure.

        A notification is always a SIDE EFFECT of some primary write — a comment,
        an assignment, a leave decision. Postgres aborts the WHOLE transaction on
        any failed statement, so without this savepoint a single bad notification
        INSERT rolls the primary write back too: posting a message on a task
        became a 500 that lost the message when the deployed DB's
        `notificationkind` enum didn't yet carry the value the code writes.

        Returns None when the insert failed, so the caller can log and carry on.
        """
        try:
            async with self._session.begin_nested():
                return await self.create(
                    recipient_id=recipient_id,
                    kind=kind,
                    level=level,
                    title=title,
                    body=body,
                    link=link,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    actor_id=actor_id,
                )
        except SQLAlchemyError:
            return None

    async def list_for_recipient(
        self,
        recipient_id: uuid.UUID,
        *,
        offset: int,
        limit: int,
        unread_only: bool,
        kind: NotificationKind | None = None,
    ) -> tuple[Sequence[Notification], int]:
        where = Notification.recipient_id == recipient_id
        if unread_only:
            where = where & Notification.read_at.is_(None)
        if kind is not None:
            where = where & (Notification.kind == kind)
        total = await self._session.scalar(
            select(func.count()).select_from(Notification).where(where)
        )
        rows = await self._session.scalars(
            select(Notification)
            # Unread first, then newest first.
            .where(where)
            .order_by(Notification.read_at.is_(None).desc(), Notification.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(rows), int(total or 0)

    async def count_unread(self, recipient_id: uuid.UUID) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.recipient_id == recipient_id, Notification.read_at.is_(None))
        )
        return int(total or 0)

    async def count_kind_since(
        self, recipient_id: uuid.UUID, kind: NotificationKind, since: datetime
    ) -> int:
        """How many notifications of `kind` this recipient got since `since`.
        Used to throttle the matching email to one per recipient per window."""
        total = await self._session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.recipient_id == recipient_id,
                Notification.kind == kind,
                Notification.created_at >= since,
            )
        )
        return int(total or 0)

    async def mark_read(
        self, recipient_id: uuid.UUID, notification_id: uuid.UUID, *, now: datetime
    ) -> bool:
        result = await self._session.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.recipient_id == recipient_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=now)
        )
        return cast("CursorResult[Any]", result).rowcount > 0

    async def mark_all_read(self, recipient_id: uuid.UUID, *, now: datetime) -> int:
        result = await self._session.execute(
            update(Notification)
            .where(Notification.recipient_id == recipient_id, Notification.read_at.is_(None))
            .values(read_at=now)
        )
        return cast("CursorResult[Any]", result).rowcount or 0

    async def recent_unread_match(
        self,
        *,
        recipient_id: uuid.UUID,
        kind: NotificationKind,
        entity_type: str | None,
        entity_id: uuid.UUID | None,
        since: datetime,
    ) -> Notification | None:
        """The most recent unread notification matching (recipient, kind, entity)
        created since `since` — used to dedupe recurring alerts."""
        stmt = (
            select(Notification)
            .where(
                Notification.recipient_id == recipient_id,
                Notification.kind == kind,
                Notification.entity_type == entity_type,
                Notification.entity_id == entity_id,
                Notification.read_at.is_(None),
                Notification.created_at >= since,
            )
            .order_by(Notification.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

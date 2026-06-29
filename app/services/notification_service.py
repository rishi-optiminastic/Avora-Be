"""Notification business rules.

Two faces:
- A read/mutate API for the inbox, always scoped to the caller as recipient.
- An internal `notify(...)` other services call to deliver a message, with
  optional dedupe so recurring alerts (e.g. underperformance) don't pile up.
No FastAPI objects here (Layering §4).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from app.models.notification import Notification, NotificationKind, NotificationLevel
from app.repositories.notification import NotificationRepository
from app.schemas.auth import CurrentUser


class NotificationService:
    def __init__(self, notifications: NotificationRepository) -> None:
        self._notifications = notifications

    # --- inbox (scoped to the caller) ------------------------------------- #
    async def list_for_caller(
        self, caller: CurrentUser, *, offset: int, limit: int, unread_only: bool
    ) -> tuple[Sequence[Notification], int]:
        return await self._notifications.list_for_recipient(
            caller.employee_id, offset=offset, limit=limit, unread_only=unread_only
        )

    async def unread_count(self, caller: CurrentUser) -> int:
        return await self._notifications.count_unread(caller.employee_id)

    async def mark_read(self, caller: CurrentUser, notification_id: uuid.UUID) -> bool:
        return await self._notifications.mark_read(
            caller.employee_id, notification_id, now=datetime.now(UTC)
        )

    async def mark_all_read(self, caller: CurrentUser) -> int:
        return await self._notifications.mark_all_read(caller.employee_id, now=datetime.now(UTC))

    # --- internal delivery (called by other services) -------------------- #
    async def notify(
        self,
        *,
        recipient_id: uuid.UUID,
        kind: NotificationKind,
        title: str,
        body: str | None = None,
        level: NotificationLevel = NotificationLevel.INFO,
        link: str | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        dedupe_within: timedelta | None = None,
    ) -> Notification | None:
        """Deliver a notification. Never notify someone about their own action
        (actor == recipient is dropped). With `dedupe_within`, skip if an unread
        notification with the same (recipient, kind, entity) already exists in
        that window — returns None when skipped."""
        if actor_id is not None and actor_id == recipient_id:
            return None
        if dedupe_within is not None:
            existing = await self._notifications.recent_unread_match(
                recipient_id=recipient_id,
                kind=kind,
                entity_type=entity_type,
                entity_id=entity_id,
                since=datetime.now(UTC) - dedupe_within,
            )
            if existing is not None:
                return None
        return await self._notifications.create(
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

    async def recent_count(
        self, *, recipient_id: uuid.UUID, kind: NotificationKind, within: timedelta
    ) -> int:
        """How many notifications of `kind` this recipient received within the
        window — lets a caller throttle a matching email to one per window."""
        return await self._notifications.count_kind_since(
            recipient_id, kind, datetime.now(UTC) - within
        )

"""Idempotency record data access.

The whole record lifecycle (claim → complete) lives in the request's own
transaction, so the stored response commits atomically with whatever the
operation did (the created task, the sent-email marker, ...). A concurrent twin
request under the same key blocks on the unique index at claim time and then
loses with an IntegrityError — it re-reads the winner's row instead of running
the operation again. See `IdempotencyKey` for the constraint.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency_key import IdempotencyKey, IdempotencyStatus


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, *, principal_id: uuid.UUID, scope: str, key: str) -> IdempotencyKey | None:
        row = await self._session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.principal_id == principal_id,
                IdempotencyKey.scope == scope,
                IdempotencyKey.idempotency_key == key,
            )
        )
        return row.scalars().first()

    async def claim(
        self, *, principal_id: uuid.UUID, scope: str, key: str, request_hash: str
    ) -> tuple[IdempotencyKey, bool]:
        """Reserve the key for this request, or return the existing record.

        Returns ``(record, is_new)``. ``is_new=True`` means this caller owns the
        key and must run the operation, then call ``complete``. ``is_new=False``
        means a prior request already holds the key (the common replay) and the
        caller should replay ``record`` instead of doing the work.
        """
        existing = await self.get(principal_id=principal_id, scope=scope, key=key)
        if existing is not None:
            return existing, False
        try:
            async with self._session.begin_nested():
                record = IdempotencyKey(
                    principal_id=principal_id,
                    scope=scope,
                    idempotency_key=key,
                    request_hash=request_hash,
                    status=IdempotencyStatus.IN_PROGRESS,
                )
                self._session.add(record)
                await self._session.flush()
            return record, True
        except IntegrityError:
            # A concurrent twin won the unique index. Re-read its row (it committed
            # while we blocked) and replay that rather than re-running the work.
            winner = await self.get(principal_id=principal_id, scope=scope, key=key)
            if winner is None:
                raise
            return winner, False

    async def complete(
        self,
        record: IdempotencyKey,
        *,
        response_json: dict[str, Any],
        response_status: int,
    ) -> None:
        record.status = IdempotencyStatus.COMPLETED
        record.response_json = response_json
        record.response_status = response_status
        self._session.add(record)
        await self._session.flush()

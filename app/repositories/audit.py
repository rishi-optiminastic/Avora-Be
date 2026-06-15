"""Audit log access — append only.

There is intentionally no update or delete method here (Security rule 5.7).
The hash chain is computed at append time from the previous entry.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


def _entry_hash(prev_hash: str | None, actor: str, action: str, target: str | None) -> str:
    canonical = f"{prev_hash or ''}|{actor}|{action}|{target or ''}"
    return hashlib.sha256(canonical.encode()).hexdigest()


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _latest_hash(self) -> str | None:
        result = await self._session.execute(
            select(AuditLog.entry_hash).order_by(desc(AuditLog.occurred_at)).limit(1)
        )
        return result.scalar_one_or_none()

    async def append(self, *, actor: str, action: str, target: str | None = None) -> AuditLog:
        prev = await self._latest_hash()
        entry = AuditLog(
            actor=actor,
            action=action,
            target=target,
            prev_hash=prev,
            entry_hash=_entry_hash(prev, actor, action, target),
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

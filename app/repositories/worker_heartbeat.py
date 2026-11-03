"""Worker heartbeat data access — record a beat, read them all back."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.worker_heartbeat import WorkerHeartbeat


class WorkerHeartbeatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def beat(self, name: str, *, now: datetime | None = None) -> WorkerHeartbeat:
        """Upsert this worker's liveness. One row per worker, forever."""
        moment = now or datetime.now(UTC)
        row = await self._session.get(WorkerHeartbeat, name)
        if row is None:
            row = WorkerHeartbeat(name=name, last_beat_at=moment)
            self._session.add(row)
        else:
            row.last_beat_at = moment
        await self._session.flush()
        return row

    async def all(self) -> Sequence[WorkerHeartbeat]:
        rows = await self._session.execute(select(WorkerHeartbeat))
        return rows.scalars().all()

"""Which background workers are alive, and which should be and are not.

`EXPECTED_WORKERS` is the assertion: these are supposed to be running. Comparing
it against the heartbeat rows turns two previously invisible failures into
reportable ones - a worker that stopped (stale row) and a worker that was never
deployed at all (no row), which no outward ping can detect because an
unconfigured ping and a healthy worker look exactly the same.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.exceptions import AuthorizationError
from app.core.workers import EXPECTED_WORKERS
from app.models.employee import Role
from app.repositories.worker_heartbeat import WorkerHeartbeatRepository
from app.schemas.auth import CurrentUser
from app.schemas.worker_health import WorkerHealthRead, WorkerHealthSummary, WorkerState


class WorkerHealthService:
    def __init__(self, heartbeats: WorkerHeartbeatRepository) -> None:
        self._heartbeats = heartbeats

    async def status(self, caller: CurrentUser) -> WorkerHealthSummary:
        if caller.role not in (Role.ADMIN, Role.HR):
            raise AuthorizationError()
        return await self.status_unscoped()

    async def status_unscoped(self, *, now: datetime | None = None) -> WorkerHealthSummary:
        """The same read without a caller, for a scheduler or an alerting job."""
        moment = now or datetime.now(UTC)
        seen = {row.name: row.last_beat_at for row in await self._heartbeats.all()}
        workers: list[WorkerHealthRead] = []
        for spec in EXPECTED_WORKERS:
            last = seen.get(spec.name)
            if last is None:
                workers.append(
                    WorkerHealthRead(
                        name=spec.name,
                        label=spec.label,
                        state=WorkerState.NEVER_SEEN,
                        last_beat_at=None,
                        minutes_since=None,
                        max_silence_minutes=spec.max_silence_minutes,
                        impact=spec.impact,
                    )
                )
                continue
            aware = last if last.tzinfo else last.replace(tzinfo=UTC)
            quiet = int((moment - aware).total_seconds() // 60)
            workers.append(
                WorkerHealthRead(
                    name=spec.name,
                    label=spec.label,
                    state=(
                        WorkerState.STALE if quiet > spec.max_silence_minutes else WorkerState.OK
                    ),
                    last_beat_at=aware,
                    minutes_since=quiet,
                    max_silence_minutes=spec.max_silence_minutes,
                    impact=spec.impact,
                )
            )
        return WorkerHealthSummary(
            checked_at=moment,
            healthy=all(w.state is WorkerState.OK for w in workers),
            workers=workers,
        )

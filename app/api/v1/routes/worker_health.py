"""Background-worker health — which schedulers are alive, and which are not.

Admin/HR only: it names every job the deployment depends on, which is a map of
the system nobody else needs.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import CurrentUserDep, WorkerHealthServiceDep
from app.schemas.worker_health import WorkerHealthSummary

router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("/health", response_model=WorkerHealthSummary)
async def worker_health(
    caller: CurrentUserDep,
    service: WorkerHealthServiceDep,
) -> WorkerHealthSummary:
    """Every expected worker with its liveness: ok, stale, or never seen."""
    return await service.status(caller)

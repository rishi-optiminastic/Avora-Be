"""Ping endpoints — managers issue (scoped); the agent polls its inbox (device)."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.core.deps import CurrentDeviceDep, CurrentUserDep, PingServiceDep
from app.schemas.ping import CaptureRequest, PingCreate, PingRead

router = APIRouter(prefix="/pings", tags=["pings"])


@router.post("", response_model=PingRead, status_code=status.HTTP_201_CREATED)
async def issue_ping(
    payload: PingCreate,
    caller: CurrentUserDep,
    service: PingServiceDep,
) -> PingRead:
    """A manager/admin pings an employee in their scope (optional message)."""
    return PingRead.model_validate(await service.issue(caller, payload))


@router.post("/capture", response_model=PingRead, status_code=status.HTTP_201_CREATED)
async def request_capture(
    payload: CaptureRequest,
    caller: CurrentUserDep,
    service: PingServiceDep,
) -> PingRead:
    """Ask an employee's agent to take a screenshot now (manager/admin, scoped)."""
    return PingRead.model_validate(await service.request_capture(caller, payload.employee_id))


@router.get("/pending", response_model=list[PingRead])
async def pending_pings(
    device: CurrentDeviceDep,
    service: PingServiceDep,
) -> list[PingRead]:
    """The agent's inbox — undelivered pings, marked delivered on fetch."""
    return [PingRead.model_validate(p) for p in await service.inbox(device)]

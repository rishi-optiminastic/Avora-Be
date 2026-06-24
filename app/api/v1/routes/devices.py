"""Device endpoints — enrollment/revocation are admin/IT only; reads are scoped."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import AgentNudgeServiceDep, CurrentUserDep, DeviceServiceDep
from app.schemas.device import (
    DeviceCreate,
    DeviceCreated,
    DeviceNudge,
    DeviceNudgeResult,
    DeviceRead,
    DeviceSelfEnroll,
    FleetUpdateResult,
)

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("", response_model=list[DeviceRead])
async def list_devices(caller: CurrentUserDep, service: DeviceServiceDep) -> list[DeviceRead]:
    devices = await service.list_for_caller(caller)
    return [DeviceRead.model_validate(d) for d in devices]


@router.post("", response_model=DeviceCreated, status_code=status.HTTP_201_CREATED)
async def enroll_device(
    payload: DeviceCreate,
    caller: CurrentUserDep,
    service: DeviceServiceDep,
) -> DeviceCreated:
    """Admin/IT enrolls a device; the raw token is returned exactly once."""
    device, raw_token = await service.enroll(caller, payload)
    return DeviceCreated(
        id=device.id,
        employee_id=device.employee_id,
        label=device.label,
        token=raw_token,
        created_at=device.created_at,
    )


@router.post("/self-enroll", response_model=DeviceCreated, status_code=status.HTTP_201_CREATED)
async def self_enroll_device(
    payload: DeviceSelfEnroll,
    caller: CurrentUserDep,
    service: DeviceServiceDep,
) -> DeviceCreated:
    """The agent enrolls the *caller's own* machine; raw token returned once."""
    device, raw_token = await service.self_enroll(caller, payload)
    return DeviceCreated(
        id=device.id,
        employee_id=device.employee_id,
        label=device.label,
        token=raw_token,
        created_at=device.created_at,
    )


@router.post("/{device_id}/revoke", response_model=DeviceRead)
async def revoke_device(
    device_id: uuid.UUID,
    caller: CurrentUserDep,
    service: DeviceServiceDep,
) -> DeviceRead:
    return DeviceRead.model_validate(await service.revoke(caller, device_id))


@router.post("/nudge", response_model=DeviceNudgeResult)
async def nudge_agent(
    payload: DeviceNudge,
    caller: CurrentUserDep,
    service: AgentNudgeServiceDep,
) -> DeviceNudgeResult:
    """Nudge an employee about their agent: an on-screen ping if it's live,
    else an in-app notification + reinstall email. Manager+ and in-scope only."""
    channel = await service.nudge(caller, payload.employee_id, payload.message)
    return DeviceNudgeResult(channel=channel)


@router.post("/update-all", response_model=FleetUpdateResult)
async def update_all_agents(
    caller: CurrentUserDep,
    service: AgentNudgeServiceDep,
) -> FleetUpdateResult:
    """Admin / IT-admin: tell every enrolled agent to self-update to the latest
    release now (instead of waiting for its periodic check)."""
    return FleetUpdateResult(updated=await service.broadcast_update(caller))

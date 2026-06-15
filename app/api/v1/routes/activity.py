"""Agent ingest endpoint.

Auth (per-device token + HMAC + rate limit) is enforced by `get_current_device`
before the handler runs. The handler hands the validated payload to the service,
which applies replay protection and server-side timestamping.
"""

import uuid
from datetime import UTC, date, datetime, time
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import (
    ActivityServiceDep,
    CurrentDeviceDep,
    CurrentUserDep,
    MonitoringServiceDep,
)
from app.schemas.activity import ActivityIngest, ActivityIngestResult, ActivitySampleRead
from app.schemas.monitoring import ActivityNowRead

router = APIRouter(prefix="/activity", tags=["activity"])


@router.post(
    "/ingest",
    response_model=ActivityIngestResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_activity(
    payload: ActivityIngest,
    device: CurrentDeviceDep,
    service: ActivityServiceDep,
) -> ActivityIngestResult:
    sample = await service.ingest(device, payload)
    return ActivityIngestResult(
        accepted=True,
        sequence=sample.sequence,
        flags=sample.flags,
    )


@router.get("/now", response_model=list[ActivityNowRead])
async def activity_now(
    caller: CurrentUserDep,
    service: MonitoringServiceDep,
) -> list[ActivityNowRead]:
    """Live state of everyone in the caller's scope (latest recent sample)."""
    return await service.live_now(caller, datetime.now(UTC))


@router.get("/{employee_id}", response_model=list[ActivitySampleRead])
async def activity_timeline(
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: MonitoringServiceDep,
    day: Annotated[date | None, Query(alias="date")] = None,
) -> list[ActivitySampleRead]:
    """One employee's raw activity samples for a day (scoped; 404 if out of scope)."""
    when = datetime.combine(day, time.min, tzinfo=UTC) if day else datetime.now(UTC)
    samples = await service.timeline(caller, employee_id, when)
    return [ActivitySampleRead.model_validate(s) for s in samples]

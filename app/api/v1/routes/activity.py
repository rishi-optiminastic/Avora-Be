"""Agent ingest endpoint.

Auth (per-device token + HMAC + rate limit) is enforced by `get_current_device`
before the handler runs. The handler hands the validated payload to the service,
which applies replay protection and server-side timestamping.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.core.deps import ActivityServiceDep, CurrentDeviceDep
from app.schemas.activity import ActivityIngest, ActivityIngestResult

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

"""Attendance endpoints — derived from raw activity, scoped to the caller."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentUserDep, MonitoringServiceDep
from app.schemas.monitoring import AttendanceRead

router = APIRouter(prefix="/attendance", tags=["attendance"])


@router.get("", response_model=list[AttendanceRead])
async def list_attendance(
    caller: CurrentUserDep,
    service: MonitoringServiceDep,
    day: Annotated[date | None, Query(alias="date")] = None,
) -> list[AttendanceRead]:
    """Per-employee attendance for a day (defaults to today)."""
    when = datetime.combine(day, time.min, tzinfo=UTC) if day else datetime.now(UTC)
    return await service.attendance(caller, when)

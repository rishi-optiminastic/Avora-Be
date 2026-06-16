"""Browsing endpoints — per-employee domain breakdown + productivity category
split, derived from raw activity and scoped to the caller."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentUserDep, MonitoringServiceDep
from app.schemas.monitoring import BrowsingRead

router = APIRouter(prefix="/browsing", tags=["browsing"])


@router.get("", response_model=list[BrowsingRead])
async def list_browsing(
    caller: CurrentUserDep,
    service: MonitoringServiceDep,
    day: Annotated[date | None, Query(alias="date")] = None,
) -> list[BrowsingRead]:
    """Per-employee browsing breakdown for a day (defaults to today)."""
    when = datetime.combine(day, time.min, tzinfo=UTC) if day else datetime.now(UTC)
    return await service.browsing(caller, when)

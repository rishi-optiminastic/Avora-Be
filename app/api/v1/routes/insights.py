"""Productivity insights — per-employee focus score (today) + 7-day trend, with
an at-risk flag. Derived from browsing activity and scoped to the caller."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.core.deps import CurrentUserDep, MonitoringServiceDep
from app.schemas.monitoring import InsightRead

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("", response_model=list[InsightRead])
async def list_insights(
    caller: CurrentUserDep,
    service: MonitoringServiceDep,
) -> list[InsightRead]:
    """Per-employee productivity rollup for the caller's scope."""
    return await service.insights(caller, datetime.now(UTC))

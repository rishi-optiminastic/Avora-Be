"""End-of-Day report endpoints.

Routes only parse input, call the service, and return a response schema (Layering
§4). Reads are scoped to the caller; edit/approve are author-only; generation is
admin-only. `response_model` is always the schema, never the ORM object (#5).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentUserDep, EodServiceDep
from app.schemas.eod import EodReportRead, EodReportUpdate

router = APIRouter(prefix="/eod", tags=["eod"])


@router.get("/me/today", response_model=EodReportRead | None)
async def my_today(caller: CurrentUserDep, service: EodServiceDep) -> EodReportRead | None:
    """The caller's own End-of-Day draft for today (null if none yet)."""
    return await service.today_for(caller, datetime.now(UTC))


@router.get("", response_model=list[EodReportRead])
async def list_reports(
    caller: CurrentUserDep,
    service: EodServiceDep,
    report_date: Annotated[str | None, Query(alias="date")] = None,
) -> list[EodReportRead]:
    """Reports for people in the caller's scope, for a day (defaults to today)."""
    return await service.list_for_scope(caller, datetime.now(UTC), report_date)


@router.get("/{report_id}", response_model=EodReportRead)
async def get_report(
    report_id: uuid.UUID, caller: CurrentUserDep, service: EodServiceDep
) -> EodReportRead:
    return await service.get_for_caller(caller, report_id)


@router.patch("/{report_id}", response_model=EodReportRead)
async def update_report(
    report_id: uuid.UUID,
    payload: EodReportUpdate,
    caller: CurrentUserDep,
    service: EodServiceDep,
) -> EodReportRead:
    """Edit the draft narrative (author only, before approval)."""
    return await service.update_draft(caller, report_id, payload.summary)


@router.post("/{report_id}/approve", response_model=EodReportRead)
async def approve_report(
    report_id: uuid.UUID, caller: CurrentUserDep, service: EodServiceDep
) -> EodReportRead:
    """Approve the draft and send it to the manager + admins (author only)."""
    return await service.approve(caller, report_id)


@router.post("/generate", response_model=int)
async def generate_reports(caller: CurrentUserDep, service: EodServiceDep) -> int:
    """Admin-only manual trigger — generate today's drafts. Returns count created."""
    return await service.generate_for_day(caller, datetime.now(UTC))

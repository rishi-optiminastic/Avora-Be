"""End-of-Day report endpoints.

Routes only parse input, call the service, and return a response schema (Layering
§4). Reads are scoped to the caller; edit/approve are author-only; generation is
admin-only. `response_model` is always the schema, never the ORM object (#5).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel

from app.core.deps import (
    CurrentUserDep,
    EodServiceDep,
    EodSettingsServiceDep,
    IdempotencyKeyHeader,
    IdempotencyServiceDep,
    SettingsDep,
)
from app.core.exceptions import NotFoundError
from app.schemas.eod import EodReportRead, EodReportUpdate
from app.schemas.eod_settings import EodSettingsRead, EodSettingsUpdate

router = APIRouter(prefix="/eod", tags=["eod"])


class EodTickResult(BaseModel):
    generated: int
    sent: int
    purged_activity: int
    purged_screenshots: int


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


@router.get("/settings", response_model=EodSettingsRead)
async def get_eod_settings(
    caller: CurrentUserDep, service: EodSettingsServiceDep
) -> EodSettingsRead:
    """The org's EOD schedule (toggle + draft/send times). Everyone may read it so
    people see when their report drafts and sends."""
    return EodSettingsRead.from_model(await service.get_or_create())


@router.put("/settings", response_model=EodSettingsRead)
async def update_eod_settings(
    payload: EodSettingsUpdate, caller: CurrentUserDep, service: EodSettingsServiceDep
) -> EodSettingsRead:
    """Change the EOD schedule (Admin/HR only — enforced in the service)."""
    return EodSettingsRead.from_model(await service.update(caller, payload))


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
async def generate_reports(
    caller: CurrentUserDep,
    service: EodServiceDep,
    idem: IdempotencyServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    """Admin-only manual trigger — generate today's drafts. Returns count created."""

    async def _op() -> int:
        return await service.generate_for_day(caller, datetime.now(UTC))

    return await idem.run(
        principal_id=caller.employee_id,
        scope="eod.generate",
        key=idempotency_key,
        request={},
        operation=_op,
    )


@router.post("/cron", response_model=EodTickResult)
async def cron_tick(
    settings: SettingsDep,
    service: EodServiceDep,
    x_cron_secret: Annotated[str | None, Header()] = None,
) -> EodTickResult:
    """Machine trigger for hosts that can't run an always-on worker (e.g. Render
    free). Authorized by the `X-Cron-Secret` header against `eod_cron_secret` —
    not a user JWT. Runs one scheduler tick (generate + auto-send). 404 when the
    secret is unset or wrong, so the endpoint's existence never leaks."""
    expected = settings.eod_cron_secret
    if not expected or not x_cron_secret or not secrets.compare_digest(x_cron_secret, expected):
        raise NotFoundError()
    # Secrets (LLM key/model) gate here; the on/off switch + schedule are in the DB
    # and applied inside run_due (so toggling EOD never needs a redeploy).
    if not settings.eod_secrets_present:
        return EodTickResult(generated=0, sent=0, purged_activity=0, purged_screenshots=0)
    generated, sent, purged_activity, purged_shots = await service.run_due(datetime.now(UTC))
    return EodTickResult(
        generated=generated,
        sent=sent,
        purged_activity=purged_activity,
        purged_screenshots=purged_shots,
    )

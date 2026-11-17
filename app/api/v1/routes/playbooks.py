"""Playbook endpoints - writing a checklist, running it for somebody, undoing it.

Authorization lives in the service (`_may_manage`), never here; HR and admins
write and run playbooks. The one exception worth reading off these signatures
is `runs_for_employee`, which a person may also call for themselves: a joiner
seeing why eleven tasks appeared on their first morning is the point.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import CurrentUserDep, PlaybookServiceDep
from app.schemas.playbook import (
    CancelResult,
    PlaybookDetail,
    PlaybookRead,
    PlaybookWrite,
    RunRead,
    RunResult,
    RunWrite,
    StepRead,
)

router = APIRouter(prefix="/playbooks", tags=["playbooks"])


def _as_read(book: object, step_count: int) -> PlaybookRead:
    read = PlaybookRead.model_validate(book)
    read.step_count = step_count
    return read


# --------------------------------------------------------------------------- #
# Runs come FIRST, before any /{playbook_id} route: FastAPI matches in
# declaration order, so `/playbooks/runs/...` would otherwise be read as
# playbook_id="runs" and answer 422. The same trap caught
# `/assessments/attempts/me`.
# --------------------------------------------------------------------------- #


@router.get("/runs/employee/{employee_id}", response_model=list[RunRead])
async def runs_for_employee(
    employee_id: uuid.UUID, caller: CurrentUserDep, service: PlaybookServiceDep
) -> list[RunRead]:
    """Which playbooks have been run for somebody. Theirs, or HR's view."""
    runs = await service.runs_for_subject(caller, employee_id)
    return [RunRead.model_validate(r) for r in runs]


@router.post("/runs/{run_id}/cancel", response_model=CancelResult)
async def cancel_run(
    run_id: uuid.UUID, caller: CurrentUserDep, service: PlaybookServiceDep
) -> CancelResult:
    """Undo a run: delete the tasks nobody started, keep the rest, free the slot."""
    removed, kept = await service.cancel_run(caller, run_id)
    return CancelResult(tasks_removed=removed, tasks_kept=kept)


@router.get("", response_model=list[PlaybookRead])
async def list_playbooks(caller: CurrentUserDep, service: PlaybookServiceDep) -> list[PlaybookRead]:
    rows = await service.list_for(caller)
    return [_as_read(book, count) for book, count in rows]


@router.post("", response_model=PlaybookDetail, status_code=status.HTTP_201_CREATED)
async def create_playbook(
    payload: PlaybookWrite, caller: CurrentUserDep, service: PlaybookServiceDep
) -> PlaybookDetail:
    """Write a playbook. Starts as a draft; publishing arms it."""
    playbook_id = await service.create_for(caller, payload)
    return await _detail(caller, service, playbook_id)


@router.get("/{playbook_id}", response_model=PlaybookDetail)
async def get_playbook(
    playbook_id: uuid.UUID, caller: CurrentUserDep, service: PlaybookServiceDep
) -> PlaybookDetail:
    return await _detail(caller, service, playbook_id)


@router.put("/{playbook_id}", response_model=PlaybookDetail)
async def update_playbook(
    playbook_id: uuid.UUID,
    payload: PlaybookWrite,
    caller: CurrentUserDep,
    service: PlaybookServiceDep,
) -> PlaybookDetail:
    """Edit it. Steps are replaced; tasks already stamped out are unaffected."""
    await service.update_for(caller, playbook_id, payload)
    return await _detail(caller, service, playbook_id)


@router.post("/{playbook_id}/publish", response_model=PlaybookDetail)
async def publish_playbook(
    playbook_id: uuid.UUID, caller: CurrentUserDep, service: PlaybookServiceDep
) -> PlaybookDetail:
    """Make it runnable, and arm an automatic trigger from today forward."""
    await service.publish_for(caller, playbook_id)
    return await _detail(caller, service, playbook_id)


@router.post("/{playbook_id}/archive", response_model=PlaybookDetail)
async def archive_playbook(
    playbook_id: uuid.UUID, caller: CurrentUserDep, service: PlaybookServiceDep
) -> PlaybookDetail:
    await service.archive_for(caller, playbook_id)
    return await _detail(caller, service, playbook_id)


@router.post("/{playbook_id}/run", response_model=RunResult)
async def run_playbook(
    playbook_id: uuid.UUID,
    payload: RunWrite,
    caller: CurrentUserDep,
    service: PlaybookServiceDep,
) -> RunResult:
    """Stamp this playbook out for one person, now."""
    outcome = await service.run_manually(caller, playbook_id, payload.subject_id, payload.anchor)
    return RunResult(run_id=outcome.run_id, created=outcome.created, skipped=list(outcome.skipped))


async def _detail(
    caller: CurrentUserDep, service: PlaybookServiceDep, playbook_id: uuid.UUID
) -> PlaybookDetail:
    book, steps = await service.detail_for(caller, playbook_id)
    detail = PlaybookDetail.model_validate(book)
    detail.steps = [StepRead.model_validate(s) for s in steps]
    detail.step_count = len(steps)
    return detail

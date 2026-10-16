"""Form endpoints: the builder and library (HR/admin), sending forms, and
filling them in. Every route is authenticated; the services scope and audit.
Anything outside the caller's reach is a 404, never a hint that it exists.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import (
    CurrentUserDep,
    FormAssignmentServiceDep,
    FormServiceDep,
    ProbationFormPanelServiceDep,
)
from app.schemas.form import (
    AssignmentCreate,
    AssignmentDetail,
    AssignmentRead,
    FormRead,
    FormResults,
    FormSummary,
    FormWrite,
    ProbationForms,
    SubmissionWrite,
)

router = APIRouter(prefix="/forms", tags=["forms"])


# --- filling in (any employee, their own) --------------------------------- #
@router.get("/assignments/mine", response_model=list[AssignmentRead])
async def my_assignments(
    caller: CurrentUserDep, service: FormAssignmentServiceDep
) -> list[AssignmentRead]:
    return await service.mine(caller)


@router.get("/assignments/needs-buddy", response_model=list[AssignmentRead])
async def joiners_without_buddy(
    caller: CurrentUserDep, service: FormAssignmentServiceDep
) -> list[AssignmentRead]:
    """HR/admin: buddy forms that can't go out because no buddy is on file."""
    return await service.needs_buddy(caller)


@router.get("/assignments/needs-manager", response_model=list[AssignmentRead])
async def probationers_without_manager(
    caller: CurrentUserDep, service: FormAssignmentServiceDep
) -> list[AssignmentRead]:
    """HR/admin: manager probation forms that can't go out because the
    probationer has no reporting manager on file."""
    return await service.needs_manager(caller)


@router.get("/probation/{employee_id}", response_model=ProbationForms)
async def probation_forms(
    employee_id: uuid.UUID, caller: CurrentUserDep, service: ProbationFormPanelServiceDep
) -> ProbationForms:
    """Where this probationer's three review forms stand. Same scope as their
    probation checklist; answers open for HR/admin and each assignee only."""
    return await service.for_employee(caller, employee_id)


@router.get("/assignments/{assignment_id}", response_model=AssignmentDetail)
async def get_assignment(
    assignment_id: uuid.UUID, caller: CurrentUserDep, service: FormAssignmentServiceDep
) -> AssignmentDetail:
    return await service.detail(caller, assignment_id)


@router.post("/assignments/{assignment_id}/submit", response_model=AssignmentDetail)
async def submit_assignment(
    assignment_id: uuid.UUID,
    payload: SubmissionWrite,
    caller: CurrentUserDep,
    service: FormAssignmentServiceDep,
) -> AssignmentDetail:
    return await service.submit(caller, assignment_id, payload)


# --- library and builder --------------------------------------------------- #
@router.get("", response_model=list[FormSummary])
async def list_forms(caller: CurrentUserDep, service: FormServiceDep) -> list[FormSummary]:
    return await service.library(caller)


@router.post("", response_model=FormRead, status_code=status.HTTP_201_CREATED)
async def create_form(
    payload: FormWrite, caller: CurrentUserDep, service: FormServiceDep
) -> FormRead:
    return await service.create(caller, payload)


@router.get("/{form_id}", response_model=FormRead)
async def get_form(form_id: uuid.UUID, caller: CurrentUserDep, service: FormServiceDep) -> FormRead:
    return await service.get(caller, form_id)


@router.put("/{form_id}", response_model=FormRead)
async def update_form(
    form_id: uuid.UUID, payload: FormWrite, caller: CurrentUserDep, service: FormServiceDep
) -> FormRead:
    return await service.update(caller, form_id, payload)


@router.delete("/{form_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(form_id: uuid.UUID, caller: CurrentUserDep, service: FormServiceDep) -> None:
    await service.delete_draft(caller, form_id)


@router.post("/{form_id}/publish", response_model=FormRead)
async def publish_form(
    form_id: uuid.UUID, caller: CurrentUserDep, service: FormServiceDep
) -> FormRead:
    return await service.publish(caller, form_id)


@router.post("/{form_id}/versions", response_model=FormRead, status_code=status.HTTP_201_CREATED)
async def new_version(
    form_id: uuid.UUID, caller: CurrentUserDep, service: FormServiceDep
) -> FormRead:
    return await service.new_version(caller, form_id)


@router.post("/{form_id}/archive", response_model=FormRead)
async def archive_form(
    form_id: uuid.UUID, caller: CurrentUserDep, service: FormServiceDep
) -> FormRead:
    return await service.archive(caller, form_id)


@router.get("/{form_id}/results", response_model=FormResults)
async def form_results(
    form_id: uuid.UUID, caller: CurrentUserDep, service: FormServiceDep
) -> FormResults:
    """HR/admin: every answer to this version, plus per-question averages."""
    return await service.results(caller, form_id)


# --- sending ---------------------------------------------------------------- #
@router.get("/{form_id}/assignments", response_model=list[AssignmentRead])
async def form_assignments(
    form_id: uuid.UUID, caller: CurrentUserDep, service: FormAssignmentServiceDep
) -> list[AssignmentRead]:
    return await service.for_form(caller, form_id)


@router.post(
    "/{form_id}/assignments",
    response_model=list[AssignmentRead],
    status_code=status.HTTP_201_CREATED,
)
async def send_form(
    form_id: uuid.UUID,
    payload: AssignmentCreate,
    caller: CurrentUserDep,
    service: FormAssignmentServiceDep,
) -> list[AssignmentRead]:
    return await service.assign(caller, form_id, payload)

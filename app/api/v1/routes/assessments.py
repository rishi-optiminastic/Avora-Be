"""CPD assessment endpoints - writing a test, assigning it, sitting it, marks.

Authorization lives in the service (`_may_manage`), never here. The one thing
worth reading off these signatures is which response model each returns:
`AssessmentDetail` and `QuestionForAuthor` carry the answer key and are HR
only, while a taker's `AttemptDetail` carries `QuestionRead`, which has no
field for it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import AssessmentServiceDep, CurrentUserDep
from app.schemas.assessment import (
    AssessmentDetail,
    AssessmentRead,
    AssessmentWrite,
    AssignmentResult,
    AssignmentWrite,
    AttemptDetail,
    AttemptRead,
    SubmissionWrite,
)

router = APIRouter(prefix="/assessments", tags=["assessments"])


@router.get("", response_model=list[AssessmentRead])
async def list_assessments(
    caller: CurrentUserDep, service: AssessmentServiceDep
) -> list[AssessmentRead]:
    """Every published test, plus HR's own drafts when HR is asking."""
    return await service.list_assessments(caller)


@router.post("", response_model=AssessmentDetail, status_code=status.HTTP_201_CREATED)
async def create_assessment(
    payload: AssessmentWrite, caller: CurrentUserDep, service: AssessmentServiceDep
) -> AssessmentDetail:
    """Write a test. HR and admins only. Starts as a draft."""
    return await service.create(caller, payload)


# --------------------------------------------------------------------------- #
# Attempts come FIRST, before any /{assessment_id} route.
#
# FastAPI matches in declaration order, so with `/{assessment_id}` above them
# a request for `/assessments/attempts/me` is matched as assessment_id =
# "attempts" and answered 422 by the UUID parser. The literal paths have to
# win, and the only thing making them win is that they are declared here.
# --------------------------------------------------------------------------- #
@router.get("/attempts/me", response_model=list[AttemptRead])
async def my_attempts(caller: CurrentUserDep, service: AssessmentServiceDep) -> list[AttemptRead]:
    """My tests and marks - what the profile history is built from."""
    return await service.history(caller, caller.employee_id)


@router.get("/attempts/employee/{employee_id}", response_model=list[AttemptRead])
async def attempts_for(
    employee_id: uuid.UUID, caller: CurrentUserDep, service: AssessmentServiceDep
) -> list[AttemptRead]:
    """Someone's history. Your own always; anyone else's for HR and admins.

    Deliberately NOT readable by a reporting manager: a test result sits
    closer to a payslip than to an attendance record.
    """
    return await service.history(caller, employee_id)


@router.get("/attempts/{attempt_id}", response_model=AttemptDetail)
async def open_attempt(
    attempt_id: uuid.UUID, caller: CurrentUserDep, service: AssessmentServiceDep
) -> AttemptDetail:
    """The paper. Questions without the key while it is unsat; the marked
    answers WITH the key once submitted, when knowing it changes nothing."""
    return await service.open_attempt(caller, attempt_id)


@router.post("/attempts/{attempt_id}/submit", response_model=AttemptDetail)
async def submit_attempt(
    attempt_id: uuid.UUID,
    payload: SubmissionWrite,
    caller: CurrentUserDep,
    service: AssessmentServiceDep,
) -> AttemptDetail:
    """Hand it in. Marks immediately and returns the result with the key.

    Only the person it belongs to may submit - HR marking on somebody's
    behalf would make the record meaningless. 409 on a second submit.
    """
    return await service.submit(caller, attempt_id, payload)


@router.get("/{assessment_id}", response_model=AssessmentDetail)
async def read_assessment(
    assessment_id: uuid.UUID, caller: CurrentUserDep, service: AssessmentServiceDep
) -> AssessmentDetail:
    """The test WITH its answer key. HR and admins only - 404 otherwise."""
    return await service.detail(caller, assessment_id)


@router.put("/{assessment_id}", response_model=AssessmentDetail)
async def update_assessment(
    assessment_id: uuid.UUID,
    payload: AssessmentWrite,
    caller: CurrentUserDep,
    service: AssessmentServiceDep,
) -> AssessmentDetail:
    """Change a test. 409 on changing the questions of a published one:
    editing the paper people have already sat makes two marks mean different
    things."""
    return await service.update(caller, assessment_id, payload)


@router.post("/{assessment_id}/publish", response_model=AssessmentDetail)
async def publish_assessment(
    assessment_id: uuid.UUID, caller: CurrentUserDep, service: AssessmentServiceDep
) -> AssessmentDetail:
    """Make it assignable. 409 when it has no questions."""
    return await service.publish(caller, assessment_id)


@router.post("/{assessment_id}/archive", response_model=AssessmentDetail)
async def archive_assessment(
    assessment_id: uuid.UUID, caller: CurrentUserDep, service: AssessmentServiceDep
) -> AssessmentDetail:
    """Stop it being assigned. Never deletes: attempts reference it, and
    somebody's record of having passed is worth keeping."""
    return await service.archive(caller, assessment_id)


@router.post(
    "/{assessment_id}/assign",
    response_model=AssignmentResult,
    status_code=status.HTTP_201_CREATED,
)
async def assign_assessment(
    assessment_id: uuid.UUID,
    payload: AssignmentWrite,
    caller: CurrentUserDep,
    service: AssessmentServiceDep,
) -> AssignmentResult:
    """Give it to everyone, some departments, or named people.

    Tops up rather than duplicating - one attempt per person per test, so
    re-assigning after a new joiner arrives is a normal thing to do.
    """
    return await service.assign(caller, assessment_id, payload)


@router.get("/{assessment_id}/results", response_model=list[AttemptRead])
async def assessment_results(
    assessment_id: uuid.UUID, caller: CurrentUserDep, service: AssessmentServiceDep
) -> list[AttemptRead]:
    """Everyone's marks for one test. HR and admins only."""
    return await service.results(caller, assessment_id)

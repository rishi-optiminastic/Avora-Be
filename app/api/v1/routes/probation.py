"""Probation review checklist endpoints.

Scope and step ownership are both enforced in the service (Golden rule #2, #3);
a route never decides who may see or move anything.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, File, Form, Path, Response, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError as PydanticValidationError

from app.core.deps import CurrentUserDep, FormAssignmentServiceDep, ProbationServiceDep
from app.core.exceptions import ValidationError
from app.core.uploads import detect_media_type
from app.models.form import FormTrigger
from app.models.probation_decision import ProbationOutcome
from app.schemas.form import AssignmentRead
from app.schemas.probation import (
    MyProbationRead,
    ProbationChecklistRead,
    ProbationDecisionCreate,
    ProbationRosterEntry,
    ProbationStepUpdate,
)
from app.services.email_service import EmailAttachment

# A signed letter is a document, not a photo: PDF only, and 10 MB is already
# generous for one.
_LETTER_TYPE = "application/pdf"
_MAX_LETTER_BYTES = 10 * 1024 * 1024

router = APIRouter(prefix="/probation", tags=["probation"])


@router.get("/roster", response_model=list[ProbationRosterEntry])
async def probation_roster(
    caller: CurrentUserDep, service: ProbationServiceDep
) -> list[ProbationRosterEntry]:
    """Everyone currently on probation, soonest confirmation first."""
    return await service.roster(caller)


@router.get("/me", response_model=MyProbationRead | None)
async def my_probation(
    caller: CurrentUserDep, service: ProbationServiceDep
) -> MyProbationRead | None:
    """Where the caller stands in their own probation.

    Declared BEFORE `/{employee_id}` so "me" is not read as a UUID.

    Deliberately not the checklist: that is HR's working document - who ticked
    what, their notes, the manager's progress - and `GET /{employee_id}` now
    refuses a self-read for exactly that reason. This is the dates, the outcome
    once there is one, and whether they still owe the acknowledgement.
    """
    return await service.mine(caller)


@router.get("/{employee_id}", response_model=ProbationChecklistRead)
async def probation_checklist(
    employee_id: uuid.UUID, caller: CurrentUserDep, service: ProbationServiceDep
) -> ProbationChecklistRead:
    return await service.checklist(caller, employee_id)


@router.put("/{employee_id}/steps/{step_no}", response_model=ProbationChecklistRead)
async def set_probation_step(
    employee_id: uuid.UUID,
    step_no: Annotated[int, Path(ge=1, le=99)],
    payload: ProbationStepUpdate,
    caller: CurrentUserDep,
    service: ProbationServiceDep,
) -> ProbationChecklistRead:
    """Move one step. Idempotent — the same status twice is the same result."""
    return await service.set_step(caller, employee_id, step_no, payload)


@router.post("/{employee_id}/decision", response_model=ProbationChecklistRead)
async def record_probation_decision(
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: ProbationServiceDep,
    outcome: Annotated[ProbationOutcome, Form()],
    effective_date: Annotated[date, Form()],
    job_title: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
    send_letter: Annotated[bool, Form()] = True,
    expectations: Annotated[list[str] | None, Form()] = None,
    letter: Annotated[UploadFile | None, File()] = None,
) -> ProbationChecklistRead:
    """Record confirmation, extension or termination, and email the letter.

    Multipart because the signed letter rides along: the email only claims an
    attachment when one is genuinely attached. `effective_date` is the
    confirmation date, the new probation end date, or the last working day,
    depending on `outcome`. Without an uploaded letter, Avora generates HR's
    standard one; an extension then needs 1-5 `expectations` (repeat the field).
    """
    attachment: EmailAttachment | None = None
    if letter is not None:
        data = await letter.read()
        if len(data) > _MAX_LETTER_BYTES:
            raise ValidationError("That letter is larger than 10 MB.")
        # Sniff the content: the browser's content-type is a claim, not a fact.
        if detect_media_type(data) != _LETTER_TYPE:
            raise ValidationError("The letter has to be a PDF.")
        attachment = EmailAttachment(
            filename=letter.filename or "probation-letter.pdf",
            content=data,
            content_type=_LETTER_TYPE,
        )
    try:
        payload = ProbationDecisionCreate(
            outcome=outcome,
            effective_date=effective_date,
            job_title=job_title,
            note=note,
            send_letter=send_letter,
            expectations=expectations or [],
        )
    except PydanticValidationError as exc:
        # Form fields are validated here rather than by FastAPI, so hand the
        # errors to the same 422 handler a JSON body would reach.
        raise RequestValidationError(
            [{**err, "loc": ("body", *err["loc"])} for err in exc.errors(include_url=False)]
        ) from exc
    return await service.decide(caller, employee_id, payload, attachment)


@router.get("/{employee_id}/decision/letter")
async def download_probation_letter(
    employee_id: uuid.UUID, caller: CurrentUserDep, service: ProbationServiceDep
) -> Response:
    """The standard confirmation or extension letter for the recorded decision.
    HR/Admin only; 404 when there is none (no decision, or a termination)."""
    pdf, filename = await service.letter_pdf(caller, employee_id)
    return Response(
        content=pdf,
        media_type=_LETTER_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/{employee_id}/forms/{trigger}/send",
    response_model=AssignmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def send_probation_form(
    employee_id: uuid.UUID,
    trigger: FormTrigger,
    caller: CurrentUserDep,
    service: FormAssignmentServiceDep,
) -> AssignmentRead:
    """Send one probation review form for this person, now.

    The same act the scheduler performs when a review window opens, for the
    cases the clock does not cover: a probation that was extended, a form
    published after someone's window opened, a manager only put on file today.
    The checklist step it owns is ticked as part of sending it.

    HR and admins only. 409 when the form is not published yet, when it has
    already gone out for this person, or when they have no probation end date.
    """
    return await service.send_probation_form(caller, employee_id, trigger)


@router.post("/me/acknowledge", response_model=MyProbationRead | None)
async def acknowledge_my_probation(
    caller: CurrentUserDep, service: ProbationServiceDep
) -> MyProbationRead | None:
    """The probationer signs off their own confirmation or extension letter.

    The one checklist step they own. A dedicated action rather than the general
    step endpoint, which reads the whole checklist to answer - exactly what
    they may not see. 409 when there is no letter to acknowledge.
    """
    return await service.acknowledge(caller)

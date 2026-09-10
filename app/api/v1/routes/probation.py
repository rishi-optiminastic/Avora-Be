"""Probation review checklist endpoints.

Scope and step ownership are both enforced in the service (Golden rule #2, #3);
a route never decides who may see or move anything.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, File, Form, Path, UploadFile

from app.core.deps import CurrentUserDep, ProbationServiceDep
from app.core.exceptions import ValidationError
from app.core.uploads import detect_media_type
from app.models.probation_decision import ProbationOutcome
from app.schemas.probation import (
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
    letter: Annotated[UploadFile | None, File()] = None,
) -> ProbationChecklistRead:
    """Record confirmation, extension or termination, and email the letter.

    Multipart because the signed letter rides along: the email only claims an
    attachment when one is genuinely attached. `effective_date` is the
    confirmation date, the new probation end date, or the last working day,
    depending on `outcome`.
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
    payload = ProbationDecisionCreate(
        outcome=outcome,
        effective_date=effective_date,
        job_title=job_title,
        note=note,
        send_letter=send_letter,
    )
    return await service.decide(caller, employee_id, payload, attachment)

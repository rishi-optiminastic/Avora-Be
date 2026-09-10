"""Probation checklist API shapes. Never the ORM model (Golden rule §5)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.core.probation import StepOwner
from app.models.probation_checklist import ProbationStepStatus
from app.models.probation_decision import ProbationOutcome


class ProbationStepRead(BaseModel):
    """One step of the checklist, merged with this employee's progress."""

    no: int
    position: int  # 1-based display order, which is not the same as `no`
    title: str
    detail: list[str]
    owner: StepOwner
    timeline: str
    status: ProbationStepStatus
    note: str | None
    actor_id: uuid.UUID | None
    actor_name: str | None
    acted_at: datetime | None
    # Whether THIS caller may move this step, so the UI can disable rather than
    # offer an action that the API will reject.
    can_update: bool


class ProbationChecklistRead(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    hire_date: date | None
    probation_end_date: date | None
    review_start_date: date | None
    days_to_probation_end: int | None
    is_on_probation: bool
    settled_steps: int
    total_steps: int
    steps: list[ProbationStepRead]
    decision: ProbationDecisionRead | None
    # Whether this caller may record the outcome and send the letter.
    can_decide: bool


class ProbationRosterEntry(BaseModel):
    """A row in the "who is on probation" list."""

    employee_id: uuid.UUID
    employee_name: str
    department: str | None
    hire_date: date | None
    probation_end_date: date | None
    days_to_probation_end: int | None
    review_due: bool
    # False once they are past their probation end date. Their finished review
    # stays listed for a while so it can still be opened and read.
    on_probation: bool
    settled_steps: int
    total_steps: int


class ProbationStepUpdate(BaseModel):
    status: ProbationStepStatus
    note: str | None = Field(default=None, max_length=1000)


class ProbationDecisionRead(BaseModel):
    outcome: ProbationOutcome
    effective_date: date
    job_title: str | None
    note: str | None
    decided_by_name: str | None
    decided_at: datetime | None
    letter_sent_at: datetime | None


class ProbationDecisionCreate(BaseModel):
    """Record the review's conclusion, and optionally email the letter.

    `effective_date` means the confirmation date, the new probation end date, or
    the last working day, depending on the outcome - see the model docstring.
    """

    outcome: ProbationOutcome
    effective_date: date
    # Named on the confirmation letter. Falls back to the employee's job title.
    job_title: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=1000)
    send_letter: bool = True

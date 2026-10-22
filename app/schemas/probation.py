"""Probation checklist API shapes. Never the ORM model (Golden rule §5)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.probation import StepOwner
from app.core.probation_letter_pdf import MAX_EXPECTATION_CHARS, MAX_EXPECTATIONS
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


class MyProbationRead(BaseModel):
    """What a probationer may see about their OWN review.

    Deliberately NOT `ProbationChecklistRead`. The 16-step checklist is HR's
    working document - who ticked what, HR's private notes, the manager's
    progress - and showing someone the machinery of their own assessment is
    both uncomfortable and none of their business. What they get is where they
    stand: the dates, the outcome once there is one, and the one step that is
    genuinely theirs to take.
    """

    employee_id: uuid.UUID
    hire_date: date | None
    probation_end_date: date | None
    review_start_date: date | None
    days_to_probation_end: int | None
    is_on_probation: bool
    # Set once HR has recorded one. Carries the letter's wording and, for an
    # extension, what is expected by the new date - things said TO them.
    decision: ProbationDecisionRead | None
    # They still owe the acknowledgement of their letter (checklist step 14).
    needs_acknowledgement: bool


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
    # EXTENDED only: the end the extension replaced, and what HR expects by the new one.
    original_end_date: date | None
    expectations: list[str]
    # Whether the standard letter can be generated for this decision, so the UI
    # only offers a download that will work.
    letter_available: bool


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
    # EXTENDED only: the numbered expectations in the extension letter. Required
    # (by the service) when Avora generates that letter rather than HR uploading one.
    expectations: list[str] = Field(default_factory=list, max_length=MAX_EXPECTATIONS)

    @field_validator("expectations")
    @classmethod
    def _clean_expectations(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("Each expectation needs some text.")
        if any(len(item) > MAX_EXPECTATION_CHARS for item in cleaned):
            raise ValueError(f"Keep each expectation to {MAX_EXPECTATION_CHARS} characters.")
        return cleaned

    @model_validator(mode="after")
    def _expectations_only_for_extensions(self) -> ProbationDecisionCreate:
        if self.expectations and self.outcome is not ProbationOutcome.EXTENDED:
            raise ValueError("Expectations only apply to a probation extension.")
        return self

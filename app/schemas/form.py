"""Form schemas (Golden rule #5 - never expose ORM models).

Write schemas validate shape here; rules that need the database (a published
form cannot change, one live form per trigger) live in the services.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, Field, model_validator

from app.models.form import AssignmentStatus, FormFieldType, FormStatus, FormTrigger
from app.models.workspace_file import WorkspaceVisibility
from app.schemas.workspace_file import AccessSpec

RATING_MIN = 1
RATING_MAX = 5
_MAX_FIELDS = 100
_MAX_OPTIONS = 20
_MAX_TEXT = 4000
_MAX_INTRO = 10_000
_MAX_ANSWER = 10_000
_MAX_TRIGGER_DAYS = 365
_MAX_DUE_DAYS = 90
_MAX_ASSIGNEES = 200
_MIN_CHOICES = 2
_MAX_OPTION = 200
_MAX_RATING_LABEL = 80
# A rating question's weight is its percent share of a 100-point score.
WEIGHT_MIN = 1
WEIGHT_TOTAL = 100

# Probation forms count `trigger_days` back from the probation end date.
PROBATION_TRIGGERS = frozenset(
    {FormTrigger.PROBATION_SELF, FormTrigger.PROBATION_MANAGER, FormTrigger.PROBATION_HR}
)

# Control characters. Postgres refuses NUL outright (a 500, not a 422), and a
# CR/LF in a title would break every email subject the title goes into.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ANY_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _one_line(value: str) -> str:
    if _ANY_CONTROL.search(value):
        raise ValueError("Use plain text on a single line.")
    return value


def _multi_line(value: str) -> str:
    """Paragraphs are welcome; invisible control characters are dropped."""
    return _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))


OneLine = Annotated[str, AfterValidator(_one_line)]
MultiLine = Annotated[str, AfterValidator(_multi_line)]


class FormFieldWrite(BaseModel):
    type: FormFieldType
    label: MultiLine = Field(min_length=1, max_length=_MAX_TEXT)
    help_text: MultiLine | None = Field(default=None, max_length=_MAX_TEXT)
    required: bool = True
    options: list[OneLine] = Field(default_factory=list, max_length=_MAX_OPTIONS)
    with_comment: bool = False
    # Rating questions only: percent share of the form's 100-point score.
    weight: int | None = Field(default=None, ge=WEIGHT_MIN, le=WEIGHT_TOTAL)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if not self.label.strip():
            raise ValueError("Every question needs some text.")
        if self.weight is not None and self.type is not FormFieldType.RATING:
            raise ValueError("Only rating questions can carry a weight.")
        # Trimmed and blanks dropped. Each option must be different (ignoring
        # case): answers are matched on the option text and results count per
        # option, so "Yes" and "yes" would split one answer in two.
        self.options = [o.strip() for o in self.options if o.strip()]
        if any(len(o) > _MAX_OPTION for o in self.options):
            raise ValueError(f"Keep each option under {_MAX_OPTION} characters.")
        if len({o.casefold() for o in self.options}) != len(self.options):
            raise ValueError("Each option must be different.")
        if self.type is FormFieldType.CHOICE and len(self.options) < _MIN_CHOICES:
            raise ValueError("A choice question needs at least two different options.")
        if self.type is not FormFieldType.CHOICE:
            self.options = []
        if self.type is not FormFieldType.RATING:
            self.with_comment = False
        if self.type is FormFieldType.SECTION:
            self.required = False  # a heading has nothing to answer
        return self


class FormWrite(AccessSpec):
    """Create a draft, or replace a draft's content."""

    visibility: WorkspaceVisibility = WorkspaceVisibility.RESTRICTED
    title: OneLine = Field(min_length=1, max_length=200)
    intro: MultiLine | None = Field(default=None, max_length=_MAX_INTRO)
    # Lowest first. Required when the form has a rating question.
    rating_labels: list[OneLine] = Field(default_factory=list, max_length=RATING_MAX)
    trigger: FormTrigger = FormTrigger.MANUAL
    trigger_days: int | None = Field(default=None, ge=1, le=_MAX_TRIGGER_DAYS)
    due_days: int = Field(default=7, ge=1, le=_MAX_DUE_DAYS)
    fields: list[FormFieldWrite] = Field(min_length=1, max_length=_MAX_FIELDS)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.title.strip():
            raise ValueError("Give the form a title.")
        self.rating_labels = [label.strip() for label in self.rating_labels]
        if any(len(label) > _MAX_RATING_LABEL for label in self.rating_labels):
            raise ValueError(f"Keep each rating label under {_MAX_RATING_LABEL} characters.")
        has_rating = any(f.type is FormFieldType.RATING for f in self.fields)
        if has_rating and (len(self.rating_labels) != RATING_MAX or not all(self.rating_labels)):
            raise ValueError("A form with rating questions needs a label for each of 1 to 5.")
        filled = [label.casefold() for label in self.rating_labels if label]
        if len(set(filled)) != len(filled):
            raise ValueError("Each point on the rating scale needs a different label.")
        if self.trigger is FormTrigger.MANUAL:
            self.trigger_days = None
        elif self.trigger_days is None and self.trigger in PROBATION_TRIGGERS:
            raise ValueError("Say how many days before the probation end date this form is sent.")
        elif self.trigger_days is None:
            raise ValueError("Say how many days after the start date this form is sent.")
        if not any(f.type is not FormFieldType.SECTION for f in self.fields):
            raise ValueError("A form needs at least one question.")
        return self


class FormFieldRead(BaseModel):
    id: uuid.UUID
    position: int
    type: FormFieldType
    label: str
    help_text: str | None
    required: bool
    options: list[str]
    with_comment: bool
    weight: int | None = None


class FormSummary(BaseModel):
    id: uuid.UUID
    family_id: uuid.UUID
    version: int
    status: FormStatus
    title: str
    trigger: FormTrigger
    trigger_days: int | None
    due_days: int
    auto_assign_since: date | None
    visibility: WorkspaceVisibility
    visible_departments: list[str]
    visible_employee_ids: list[uuid.UUID]
    published_at: datetime | None
    updated_at: datetime
    question_count: int
    # HR/admin only; zero for everyone else.
    response_count: int = 0
    pending_count: int = 0


class FormRead(FormSummary):
    intro: str | None
    rating_labels: list[str]
    fields: list[FormFieldRead]


class AssignmentCreate(BaseModel):
    """HR sends a published form to people by hand."""

    assignee_ids: list[uuid.UUID] = Field(min_length=1, max_length=_MAX_ASSIGNEES)
    # Who the form is about, when it is about someone else.
    subject_id: uuid.UUID | None = None
    due_on: date | None = None


class AssignmentRead(BaseModel):
    id: uuid.UUID
    form_id: uuid.UUID
    form_title: str
    assignee_id: uuid.UUID | None
    assignee_name: str | None
    subject_id: uuid.UUID | None
    subject_name: str | None
    trigger: FormTrigger
    status: AssignmentStatus
    available_on: date
    due_on: date | None
    submitted_at: datetime | None


class AnswerWrite(BaseModel):
    field_id: uuid.UUID
    rating: int | None = Field(default=None, ge=RATING_MIN, le=RATING_MAX)
    text: MultiLine | None = Field(default=None, max_length=_MAX_ANSWER)
    choice: OneLine | None = Field(default=None, max_length=_MAX_OPTION)


class SubmissionWrite(BaseModel):
    answers: list[AnswerWrite] = Field(max_length=_MAX_FIELDS)

    @model_validator(mode="after")
    def _one_answer_per_question(self) -> Self:
        ids = [a.field_id for a in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError("Each question can be answered only once.")
        return self


class AnswerRead(BaseModel):
    field_id: uuid.UUID
    rating: int | None
    text: str | None
    choice: str | None
    # (rating / 5) * the question's weight, for a weighted rating question.
    weighted_score: float | None = None


class SubjectDetails(BaseModel):
    """Read-only facts about the person a probation form is about, taken from
    Avora rather than typed in. Only what the form itself names."""

    employee_name: str
    department: str | None
    designation: str | None
    reporting_manager: str | None
    date_of_joining: date | None
    probation_end_date: date | None


class AssignmentDetail(AssignmentRead):
    form: FormRead
    answers: list[AnswerRead]
    # Out of 100, when the form has weighted questions and has been answered.
    weighted_total: float | None = None
    subject_details: SubjectDetails | None = None


class FieldResult(BaseModel):
    """One question's results across every response to a form version."""

    field_id: uuid.UUID
    label: str
    type: FormFieldType
    answered: int
    average: float | None = None
    weight: int | None = None
    # Rating "1".."5" or choice option -> how many picked it.
    counts: dict[str, int] = Field(default_factory=dict)


class ResponseRead(BaseModel):
    assignment_id: uuid.UUID
    respondent_id: uuid.UUID | None
    respondent_name: str | None
    subject_id: uuid.UUID | None
    subject_name: str | None
    submitted_at: datetime
    answers: list[AnswerRead]
    # Out of 100, when the form has weighted questions.
    weighted_total: float | None = None
    subject_details: SubjectDetails | None = None


class FormResults(BaseModel):
    form: FormRead
    fields: list[FieldResult]
    responses: list[ResponseRead]


class ProbationFormStatus(BaseModel):
    """Where one of the three probation forms stands for one probationer."""

    trigger: FormTrigger
    # None when no form is live for this trigger and none was ever sent.
    form_title: str | None
    # None while it has not gone out yet.
    assignment_id: uuid.UUID | None = None
    status: AssignmentStatus | None = None
    assignee_name: str | None = None
    available_on: date | None = None
    due_on: date | None = None
    submitted_at: datetime | None = None
    weighted_total: float | None = None
    # Whether this caller may open the answers (HR/admin, or the assignee).
    can_open: bool = False


class ProbationForms(BaseModel):
    employee_id: uuid.UUID
    forms: list[ProbationFormStatus]

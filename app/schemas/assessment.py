"""CPD assessment request and response shapes.

The split that matters is `QuestionRead` versus `QuestionForAuthor`. A taker's
question carries no `correct` field at all - not an empty one, not a hidden
one - so there is no code path that can serialise the answer key to the
person sitting the test. HR's own view is the separate schema below.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.assessment import AssessmentStatus, AttemptStatus, QuestionKind
from app.schemas.audience import AudienceSpec

MAX_OPTIONS = 10
MAX_QUESTIONS = 100


class QuestionWrite(BaseModel):
    """One question as HR writes it, answer key included."""

    prompt: str = Field(min_length=1, max_length=1000)
    kind: QuestionKind = QuestionKind.SINGLE
    options: list[str] = Field(min_length=2, max_length=MAX_OPTIONS)
    #: Indexes into `options`. EMPTY means "not keyed yet" - see
    #: `_key_is_answerable`.
    correct: list[int] = Field(default_factory=list, max_length=MAX_OPTIONS)
    points: int = Field(default=1, ge=0, le=100)
    #: Which competency this question tests. A section heading, not a hint.
    competency: str | None = Field(default=None, max_length=80)

    @field_validator("options")
    @classmethod
    def _real_options(cls, value: list[str]) -> list[str]:
        trimmed = [v.strip() for v in value]
        if any(not v for v in trimmed):
            raise ValueError("Every option needs text.")
        if len({v.casefold() for v in trimmed}) != len(trimmed):
            raise ValueError("Two options are the same.")
        return trimmed

    @model_validator(mode="after")
    def _key_is_answerable(self) -> QuestionWrite:
        """The key must point at real options, and match the kind.

        Caught here rather than at marking time: a SINGLE question with two
        correct answers is unanswerable, and discovering that after thirty
        people have sat the test is too late to fix it fairly.
        """
        key = set(self.correct)
        if not key:
            # No key YET, which a DRAFT is allowed to be: a paper can be
            # written - or imported from a document that never carried one -
            # before anybody decides the answers. `publish` is what refuses
            # to let an unkeyed question be sat, because marking one scores
            # everybody wrong (see `app.core.marking.is_right`).
            return self
        if any(i < 0 or i >= len(self.options) for i in key):
            raise ValueError("The correct answer must be one of the options.")
        if self.kind is QuestionKind.SINGLE and len(key) != 1:
            raise ValueError("A single-answer question needs exactly one correct option.")
        if self.kind is QuestionKind.MULTI and len(key) < 2:
            raise ValueError(
                "A multiple-answer question needs at least two correct options - "
                "use a single-answer question otherwise."
            )
        if len(key) == len(self.options):
            raise ValueError("Every option cannot be correct.")
        return self


class AssessmentWrite(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    #: The department this paper is written for. A grouping label; `assign`
    #: still decides who gets it.
    department: str | None = Field(default=None, max_length=120)
    pass_mark: int = Field(default=60, ge=0, le=100)
    time_limit_minutes: int | None = Field(default=None, ge=1, le=600)
    questions: list[QuestionWrite] = Field(default_factory=list, max_length=MAX_QUESTIONS)

    @field_validator("title")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Give the assessment a title.")
        return trimmed


class QuestionRead(BaseModel):
    """A question as the person SITTING the test sees it.

    No `correct` field exists on this model. That is the whole point: there
    is no path, however the service is called, that puts the answer key in
    front of a taker.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    prompt: str
    kind: QuestionKind
    options: list[str]
    points: int
    competency: str | None = None


class QuestionForAuthor(QuestionRead):
    """HR's view: the same question, with the key."""

    correct: list[int]


class AssessmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    department: str | None = None
    status: AssessmentStatus
    pass_mark: int
    time_limit_minutes: int | None
    question_count: int = 0
    total_points: int = 0
    #: Questions still without an answer key. Any at all and the test cannot
    #: be published, so the list can say why rather than offering a button
    #: that exists only to answer 409.
    unkeyed_count: int = 0
    #: How many people hold it, and how many have sat it. HR's at-a-glance.
    assigned_count: int = 0
    submitted_count: int = 0
    created_at: datetime


class AssessmentDetail(AssessmentRead):
    """HR's full view, answer key included."""

    questions: list[QuestionForAuthor] = []


class AssignmentWrite(BaseModel):
    """Who to give it to. The same audience shape as events and form sends."""

    audience: AudienceSpec
    due_on: datetime | None = None


class AssignmentResult(BaseModel):
    """What an assignment actually did.

    Counted rather than assumed: re-assigning tops up the people who do not
    hold it, so "sent to 12" and "4 already had it" are both true and only
    one of them is news.
    """

    assigned: int
    already_held: int


class AnswerWrite(BaseModel):
    question_id: uuid.UUID
    #: Indexes into the question's options. Empty means unanswered.
    chosen: list[int] = Field(default_factory=list, max_length=MAX_OPTIONS)


class SubmissionWrite(BaseModel):
    answers: list[AnswerWrite] = Field(default_factory=list, max_length=MAX_QUESTIONS)


class AnswerResult(BaseModel):
    """One marked answer, for the review screen after submitting."""

    question_id: uuid.UUID
    prompt: str
    competency: str | None = None
    options: list[str]
    chosen: list[int]
    is_correct: bool
    points_awarded: float
    #: What the question was worth. `points_awarded` alone cannot be read as
    #: a score - a per-competency breakdown needs the denominator too, and
    #: deriving it from "awarded or zero" loses it for every wrong answer.
    points: int = 1
    #: Shown only once the attempt is submitted, and never before.
    correct: list[int] | None = None


class AttemptRead(BaseModel):
    """One sitting, as a row in a history or a results table."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    assessment_id: uuid.UUID
    assessment_title: str = ""
    #: The mark this attempt was judged against. Snapshotted onto the row at
    #: submission; the service fills in the test's live one before that.
    pass_mark: int = 0

    @field_validator("pass_mark", mode="before")
    @classmethod
    def _unsat_has_no_mark_yet(cls, value: object) -> object:
        """The column is null until the paper is submitted.

        `model_validate` reads the ORM attribute before the service replaces
        it, so without this an unsat attempt fails validation on its own
        snapshot field. Zero only ever exists between those two steps.
        """
        return 0 if value is None else value
    employee_id: uuid.UUID
    employee_name: str | None = None
    status: AttemptStatus
    score: float | None
    max_score: float | None
    percentage: float | None
    passed: bool | None
    due_on: datetime | None
    submitted_at: datetime | None
    created_at: datetime


class AttemptDetail(AttemptRead):
    """One sitting, opened.

    `questions` is the taker's view while it is unsubmitted, and `answers`
    replaces it once marked - a paper is either being sat or being reviewed,
    never both.
    """

    questions: list[QuestionRead] = []
    answers: list[AnswerResult] = []
    description: str | None = None
    time_limit_minutes: int | None = None

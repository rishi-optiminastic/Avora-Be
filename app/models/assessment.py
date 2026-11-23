"""CPD assessments — a test HR writes, assigns, and marks automatically.

Deliberately NOT built on `Form`. The forms engine scores by weight times a
1-5 rating: a subjective judgement about a person. A test scores by right or
wrong against an answer key. They look similar on screen and are different
underneath, and the thing that settles it is the answer key: a `FormField`
has nowhere to put one, and bolting it on would make the correct answers
readable by everyone who can already read a form.

What IS reused is `AudienceResolver` - "everyone / these departments / these
people" is already answered once for events and form sends, and a test needs
exactly the same question answered.

Three rules the schema enforces rather than trusts:

* **The answer key never travels with the questions.** `correct` lives on the
  question row and the taker's read schema has no field for it.
* **A result is a snapshot.** `score`, `max_score` and `passed` are written
  onto the attempt when it is submitted, never recomputed from the questions.
  A question edited or a pass mark raised afterwards must not silently change
  what somebody already scored.
* **One attempt per person per assessment.** Re-assigning tops up the people
  who do not have it rather than handing anyone a second copy.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AssessmentStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    # Stops it being assigned again. Never deleted: attempts reference it and
    # somebody's record of having passed is worth keeping.
    ARCHIVED = "archived"


class QuestionKind(StrEnum):
    """Both are marked automatically, which is the point of a test.

    A written answer would need a human marking queue - a different feature,
    and one that would stop HR seeing results the moment the test closes.
    """

    #: Exactly one option is right.
    SINGLE = "single"
    #: Several are right, and the taker must pick all of them and nothing else.
    MULTI = "multi"


class AttemptStatus(StrEnum):
    ASSIGNED = "assigned"
    SUBMITTED = "submitted"


class Assessment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assessments"
    __table_args__ = (Index("ix_assessments_status_created", "status", "created_at"),)

    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000), default=None)
    status: Mapped[AssessmentStatus] = mapped_column(default=AssessmentStatus.DRAFT)
    #: Percentage needed to pass, 0-100.
    pass_mark: Mapped[int] = mapped_column(Integer, default=60)
    #: None = no limit. Minutes, enforced as guidance on screen rather than a
    #: hard server cutoff: a cutoff would discard honest work on a dropped
    #: connection, which is worse than a generous test.
    time_limit_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    #: The department this paper is written FOR, as the same plain label
    #: `employees.department` uses. Nullable: a test can be for everybody.
    #:
    #: It is a grouping label and nothing more - `assign` still decides who
    #: actually gets the test. Scoping on it instead would mean a department
    #: renamed in the directory silently stopped matching, turning a live
    #: paper into one nobody can be given.
    department: Mapped[str | None] = mapped_column(String(120), default=None)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class AssessmentQuestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One question and its answer key."""

    __tablename__ = "assessment_questions"
    __table_args__ = (
        UniqueConstraint("assessment_id", "position", name="uq_assessment_question_position"),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(String(1000))
    kind: Mapped[QuestionKind] = mapped_column(default=QuestionKind.SINGLE)
    #: The choices, in the order they are shown.
    options: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: THE ANSWER KEY: indexes into `options`. Never serialised to a taker -
    #: `QuestionRead` has no field for it, and only the author's schema does.
    correct: Mapped[list[int]] = mapped_column(JSON, default=list)
    #: What this question is worth. Lets a test weight the hard ones.
    points: Mapped[int] = mapped_column(Integer, default=1)
    #: Which competency this question tests - "Problem Solving".
    #:
    #: A label rather than an enum, because the five CPD competencies are
    #: HR's wording and a sixth should not need a migration. Safe to put in
    #: front of a taker: it is the section heading they are already reading,
    #: not a hint at the key.
    competency: Mapped[str | None] = mapped_column(String(80), default=None)


class AssessmentAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One person's sitting of one assessment."""

    __tablename__ = "assessment_attempts"
    __table_args__ = (
        UniqueConstraint("assessment_id", "employee_id", name="uq_attempt_assessment_employee"),
        Index("ix_attempts_employee_status", "employee_id", "status"),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[AttemptStatus] = mapped_column(default=AttemptStatus.ASSIGNED)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    due_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # --- the result, SNAPSHOT at submission (see the module docstring) ----- #
    score: Mapped[float | None] = mapped_column(Float, default=None)
    max_score: Mapped[float | None] = mapped_column(Float, default=None)
    #: Percentage, so a history list does not have to divide to rank attempts.
    percentage: Mapped[float | None] = mapped_column(Float, default=None)
    passed: Mapped[bool | None] = mapped_column(Boolean, default=None)
    #: The pass mark this attempt was JUDGED AGAINST, snapshotted with the
    #: verdict. Reading the assessment's current one instead made a history
    #: row contradict itself the moment HR changed it: "55%, pass mark 50%,
    #: Not passed". Null until submitted, when the live one still applies.
    pass_mark: Mapped[int | None] = mapped_column(Integer, default=None)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class AssessmentAnswer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """What one person picked for one question, and whether it was right."""

    __tablename__ = "assessment_answers"
    __table_args__ = (
        UniqueConstraint("attempt_id", "question_id", name="uq_answer_attempt_question"),
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_attempts.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_questions.id", ondelete="CASCADE"), index=True
    )
    #: Indexes into the question's `options`.
    chosen: Mapped[list[int]] = mapped_column(JSON, default=list)
    #: Marked once, at submission, and stored - so a later edit to the answer
    #: key cannot retroactively make somebody's answer wrong.
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False)
    points_awarded: Mapped[float] = mapped_column(Float, default=0.0)

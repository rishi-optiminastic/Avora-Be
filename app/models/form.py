"""Forms: HR-built questionnaires, who has to fill them in, and their answers.

`Form` is one VERSION of a form. Versions of the same form share a `family_id`.
A draft is edited in place; a published version is never edited again, because
answers are only meaningful against the exact questions that were asked. To
change a published form, HR starts a new draft version from it; publishing that
retires the old one, and the old one's answers keep pointing at it.

`FormAssignment` is "this person must fill in this form", optionally about
someone else (a buddy writing about the joiner they looked after). Assignments
made automatically carry a `trigger`, and there is at most ONE per (trigger,
subject) ever, so a rejoiner is never sent a form a second time and the daily
job is idempotent.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.workspace_file import WorkspaceVisibility


class FormStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    # A published version that a newer version replaced, or that HR withdrew.
    # Its assignments and answers stay readable.
    ARCHIVED = "archived"


class FormTrigger(StrEnum):
    """What makes Avora send a form on its own. MANUAL = only when HR sends it."""

    MANUAL = "manual"
    # To the joiner, a set number of days after their start date.
    NEW_JOINER = "new_joiner"
    # To the joiner's buddy, about the joiner, a set number of days after the
    # joiner's start date.
    BUDDY = "buddy"
    # The three probation review forms. For these `trigger_days` counts days
    # BEFORE the probationer's probation end date, not after the start date.
    # To the probationer themselves.
    PROBATION_SELF = "probation_self"
    # To the probationer's current reporting manager, about them.
    PROBATION_MANAGER = "probation_manager"
    # To nobody in particular: one shared assignment per probationer in the HR
    # pool. Any HR or admin may fill it in; whoever submits first claims it.
    PROBATION_HR = "probation_hr"


class FormFieldType(StrEnum):
    # Not a question: a heading plus optional intro text that groups the
    # questions after it ("Section B. Buddy System").
    SECTION = "section"
    # 1-5, labelled by the form's `rating_labels`; `with_comment` adds a
    # written answer to the same question ("1-5 Rating & Para").
    RATING = "rating"
    # A written answer.
    PARAGRAPH = "paragraph"
    # One of `options`.
    CHOICE = "choice"


class AssignmentStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    # A buddy form whose joiner has no buddy on file yet. Nobody can fill it
    # in; HR is told, and it becomes PENDING once a buddy is set.
    NEEDS_BUDDY = "needs_buddy"
    # A manager probation form whose probationer has no reporting manager on
    # file. Same handling as NEEDS_BUDDY: HR is told once, and it becomes
    # PENDING once a manager is set.
    NEEDS_MANAGER = "needs_manager"
    CANCELLED = "cancelled"


class Form(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "forms"
    __table_args__ = (
        UniqueConstraint("family_id", "version", name="uq_forms_family_version"),
        Index("ix_forms_status_trigger", "status", "trigger"),
        # One live form per automatic trigger, enforced by the database so two
        # racing publishes can't both win (the service turns it into a 409).
        Index(
            "uq_forms_live_trigger",
            "trigger",
            unique=True,
            postgresql_where=text("status = 'PUBLISHED' AND trigger <> 'MANUAL'"),
            sqlite_where=text("status = 'PUBLISHED' AND trigger <> 'MANUAL'"),
        ),
    )

    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[FormStatus] = mapped_column(default=FormStatus.DRAFT)

    title: Mapped[str] = mapped_column(String(200))
    # Shown above the questions. Long on purpose: the onboarding forms open
    # with several paragraphs explaining why the feedback is wanted.
    intro: Mapped[str | None] = mapped_column(Text, default=None)
    # Five labels for the 1-5 scale, lowest first ("Poor" .. "Excellent").
    rating_labels: Mapped[list[str]] = mapped_column(JSON, default=list)

    trigger: Mapped[FormTrigger] = mapped_column(default=FormTrigger.MANUAL)
    # Days after the joiner's start date the form opens (joiner forms), or days
    # before the probation end date (probation forms). Triggered forms only.
    trigger_days: Mapped[int | None] = mapped_column(Integer, default=None)
    # Days the person has to answer once it opens (shown as a due date only).
    due_days: Mapped[int] = mapped_column(Integer, default=7)
    # Joiners who started before this date are never sent the form
    # automatically, so publishing it does not mail the whole company. Set
    # when a triggered form is first published; new versions keep it. For the
    # probation forms it is compared to the probation END date instead: only
    # probations ending after it are reviewed.
    auto_assign_since: Mapped[date | None] = mapped_column(Date, default=None)

    # Who can see the form in the library (not its answers: those are HR and
    # admin only). Same model as workspace files.
    visibility: Mapped[WorkspaceVisibility] = mapped_column(default=WorkspaceVisibility.RESTRICTED)
    visible_departments: Mapped[list[str]] = mapped_column(JSON, default=list)
    visible_employee_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class FormField(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "form_fields"
    __table_args__ = (UniqueConstraint("form_id", "position", name="uq_form_fields_position"),)

    form_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("forms.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    type: Mapped[FormFieldType] = mapped_column()
    # The question, or the section heading.
    label: Mapped[str] = mapped_column(Text)
    # Extra text under the label: section intro or question guidance.
    help_text: Mapped[str | None] = mapped_column(Text, default=None)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    options: Mapped[list[str]] = mapped_column(JSON, default=list)
    with_comment: Mapped[bool] = mapped_column(Boolean, default=False)
    # Rating questions only: this question's share of a 100-point weighted
    # score, in percent. A form's weights add up to 100 once it is published.
    weight: Mapped[int | None] = mapped_column(Integer, default=None)


class FormAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "form_assignments"
    __table_args__ = (
        # One automatic assignment per (trigger, subject), ever.
        Index(
            "uq_form_assignments_trigger_subject",
            "trigger",
            "subject_id",
            unique=True,
            postgresql_where=text("trigger <> 'MANUAL'"),
            sqlite_where=text("trigger <> 'MANUAL'"),
        ),
        Index("ix_form_assignments_assignee_status", "assignee_id", "status"),
    )

    form_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("forms.id", ondelete="CASCADE"), index=True
    )
    # Who fills it in. Empty while a buddy or manager form waits for that
    # person, and for an HR probation form until someone in HR submits it.
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), default=None
    )
    # Who it is about: the joiner, for both onboarding forms.
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), default=None, index=True
    )
    trigger: Mapped[FormTrigger] = mapped_column(default=FormTrigger.MANUAL)
    status: Mapped[AssignmentStatus] = mapped_column(default=AssignmentStatus.PENDING)
    # Hidden from the assignee before this day.
    available_on: Mapped[date] = mapped_column(Date)
    due_on: Mapped[date | None] = mapped_column(Date, default=None)

    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    # Set once the assignee has been told (email + in-app). Telling is
    # best-effort and never undoes the assignment.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Set once HR has been told this joiner has no buddy (or this probationer
    # has no manager).
    hr_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class FormResponse(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "form_responses"

    assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("form_assignments.id", ondelete="CASCADE"), unique=True
    )
    form_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("forms.id", ondelete="CASCADE"), index=True
    )
    respondent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FormAnswer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "form_answers"
    __table_args__ = (
        UniqueConstraint("response_id", "field_id", name="uq_form_answers_response_field"),
    )

    response_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("form_responses.id", ondelete="CASCADE"), index=True
    )
    field_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("form_fields.id", ondelete="CASCADE"), index=True
    )
    rating: Mapped[int | None] = mapped_column(Integer, default=None)
    text: Mapped[str | None] = mapped_column(Text, default=None)
    choice: Mapped[str | None] = mapped_column(String(200), default=None)

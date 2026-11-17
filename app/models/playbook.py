"""Playbooks: a checklist written once, stamped out per person as real tasks.

`Playbook` is the template, `PlaybookStep` one task in it, `PlaybookRun` one
instantiation - "the new-joiner playbook, for Aditi, anchored on her start
date". Tasks created by a run point back at it through `tasks.playbook_run_id`,
so "where did this come from?" has an answer and a bad run can be found again.

Measured before this was built: 74 onboarding-shaped tasks typed by hand by
one HR lead across 41 distinct steps, 32 more by another. The steps never
changed; only the name did.

**Deliberately the same shape as `Form`'s triggers**, because this is the same
problem - something fires off an employee's lifecycle date and must fire
exactly once. Three constraints carried over, each paid for:

* **One PUBLISHED playbook per automatic trigger** (`uq_playbooks_live_trigger`),
  so "the new-joiner playbook" is never ambiguous.
* **One run per (trigger, subject)** (`uq_playbook_runs_trigger_subject`), so a
  rejoiner is not onboarded twice and the sweep is idempotent.
* **`auto_assign_since`**, the backfill guard. Without it, publishing a
  new-joiner playbook fires for everybody already on the roster - the same
  shape of accident as the 117 tasks a naive recurrence switch-on would have
  created.

**One constraint deliberately NOT copied.** `uq_form_assignments_trigger_subject`
does not exclude cancelled rows, which is exactly why a probation form could
not be cancelled and re-sent - the row had to be reused instead. The run
constraint here excludes CANCELLED, so a run can be undone and done again.
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import (
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.task import TaskPriority


class PlaybookTrigger(StrEnum):
    """What makes Avora run a playbook on its own.

    Only two for now, on purpose. MANUAL carries the measured value (HR picks
    the playbook and the person), and NEW_JOINER automates the one lifecycle
    event the data shows being handled by hand every time. Probation already
    has its own checklist module; pointing a playbook at it as well would
    give two things the same job.
    """

    MANUAL = "manual"
    #: N days after the joiner's start date (`trigger_days`, usually 0).
    NEW_JOINER = "new_joiner"
    #: N days before an accepted resignation's last working day. The mirror
    #: of NEW_JOINER, and the same measured problem: an exit checklist retyped
    #: per leaver.
    OFFBOARDING = "offboarding"
    #: Repeating, keyed by month rather than firing once. MONTH_START anchors
    #: on the 1st and fires `trigger_days` later; MONTH_END anchors on the
    #: last day and fires `trigger_days` before it. This is what covers the
    #: monthly compliance set (GSTR 1, GSTR 3B, TDS, salary entry) that the
    #: live data shows being recreated by hand every month.
    MONTH_START = "month_start"
    MONTH_END = "month_end"


class PlaybookStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class AssigneeRule(StrEnum):
    """Who a step's task goes to, resolved per run rather than stored.

    A rule, not an employee id, because the answer differs per subject: "the
    joiner's manager" is a different person for each joiner, and freezing one
    name into the template would send Aditi's manager Sahil's tasks.
    """

    #: The person the run is about - the joiner themselves.
    SUBJECT = "subject"
    SUBJECT_MANAGER = "subject_manager"
    SUBJECT_BUDDY = "subject_buddy"
    #: A named person, for steps that always belong to the same desk - the HR
    #: lead who sends the welcome kit, whoever the joiner is.
    FIXED = "fixed"


class PlaybookRunStatus(StrEnum):
    COMPLETED = "completed"
    #: Undone. Its tasks are deleted with it, and it stops blocking a re-run.
    CANCELLED = "cancelled"


class Playbook(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "playbooks"
    __table_args__ = (
        Index("ix_playbooks_status_trigger", "status", "trigger"),
        # One live playbook per LIFECYCLE trigger, enforced by the database so
        # two racing publishes cannot both win (the service turns it into 409).
        #
        # Monthly triggers are deliberately excluded: finance and ops can each
        # own a month-end checklist, and "the month-end playbook" is not a
        # meaningful single thing the way "the new-joiner playbook" is. The
        # monthly sweep iterates every published one instead of picking.
        Index(
            "uq_playbooks_live_trigger",
            "trigger",
            unique=True,
            postgresql_where=text(
                "status = 'PUBLISHED' AND trigger IN ('NEW_JOINER', 'OFFBOARDING')"
            ),
            sqlite_where=text("status = 'PUBLISHED' AND trigger IN ('NEW_JOINER', 'OFFBOARDING')"),
        ),
    )

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[PlaybookStatus] = mapped_column(default=PlaybookStatus.DRAFT)
    trigger: Mapped[PlaybookTrigger] = mapped_column(default=PlaybookTrigger.MANUAL)
    #: Days after the subject's anchor date before the run fires. Null for
    #: MANUAL, which fires when somebody clicks.
    trigger_days: Mapped[int | None] = mapped_column(Integer, default=None)
    #: THE BACKFILL GUARD. Only subjects whose anchor date is on or after this
    #: are picked up automatically, so publishing does not fire for everybody
    #: already on the roster. Stamped when a triggered playbook is published.
    auto_assign_since: Mapped[date | None] = mapped_column(Date, default=None)
    #: Who a non-lifecycle playbook is FOR.
    #:
    #: A new-joiner playbook is about whoever joined, so the subject comes
    #: from the event. A monthly compliance checklist is about nobody - it
    #: belongs to a desk - so the subject has to be named on the playbook
    #: itself. Null for lifecycle triggers, required for monthly ones.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    published_at: Mapped[date | None] = mapped_column(Date, default=None)


class PlaybookStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "playbook_steps"
    __table_args__ = (Index("ix_playbook_steps_playbook_position", "playbook_id", "position"),)

    playbook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("playbooks.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    #: May contain `{name}`, `{first_name}`, `{department}`, `{job_title}`,
    #: `{start_date}` - see `app/core/playbook.py`, which also validates them.
    title_template: Mapped[str] = mapped_column(String(256))
    description_template: Mapped[str | None] = mapped_column(String(2000), default=None)
    assignee_rule: Mapped[AssigneeRule] = mapped_column(default=AssigneeRule.SUBJECT)
    #: Only for AssigneeRule.FIXED.
    fixed_assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    #: Days from the run's anchor to this task's due date. Negative is valid -
    #: "ship the laptop" belongs before day one.
    offset_days: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[TaskPriority] = mapped_column(default=TaskPriority.MEDIUM)
    expected_output: Mapped[str | None] = mapped_column(String(2000), default=None)


class PlaybookRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "playbook_runs"
    __table_args__ = (
        # Idempotency, keyed by PERIOD rather than hard-coded to once-ever.
        #
        # `period_key` is "" for a one-shot trigger - a person is onboarded
        # once - and "YYYY-MM" for a monthly one, so November's close and
        # December's are different runs and neither can happen twice. Without
        # the period in the key a monthly playbook would refuse to run again
        # after its first month.
        #
        # Still EXCLUDES cancelled, which is the fix for the forms trap.
        Index(
            "uq_playbook_runs_trigger_subject",
            "trigger",
            "subject_id",
            "period_key",
            unique=True,
            postgresql_where=text("trigger <> 'MANUAL' AND status <> 'CANCELLED'"),
            sqlite_where=text("trigger <> 'MANUAL' AND status <> 'CANCELLED'"),
        ),
        Index("ix_playbook_runs_subject", "subject_id"),
    )

    playbook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("playbooks.id", ondelete="CASCADE"), index=True
    )
    #: Who the run is about. Not the assignee - a run about Aditi creates
    #: tasks for Aditi, her manager and the HR lead.
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    #: Copied from the playbook at run time, because uniqueness is per
    #: trigger: republishing the playbook must not let a joiner run twice.
    trigger: Mapped[PlaybookTrigger] = mapped_column(default=PlaybookTrigger.MANUAL)
    status: Mapped[PlaybookRunStatus] = mapped_column(default=PlaybookRunStatus.COMPLETED)
    #: The date every step's offset is counted from - the joiner's start date,
    #: NOT the day the worker ran. Stored so a run is explainable later.
    anchor_date: Mapped[date] = mapped_column(Date)
    #: What counts as a repeat: "" for once-ever, "YYYY-MM" for monthly.
    #: Part of the uniqueness key - see the index above.
    period_key: Mapped[str] = mapped_column(String(16), default="")
    tasks_created: Mapped[int] = mapped_column(Integer, default=0)
    ran_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )

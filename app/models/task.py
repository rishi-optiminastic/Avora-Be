"""Task — assigned work tracked against deadlines and projects.

A task is assigned BY one employee (a manager/admin, or the employee themselves)
TO an employee. Reads are scoped to the caller exactly like employees: you see
your own tasks, your reports'/department's tasks, or everything (admin/HR), plus
anything you assigned (Security rule 5.3 — scope enforced in the repository).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.employee import Role

if TYPE_CHECKING:
    from app.models.employee import Employee
    from app.models.target import Target
    from app.models.work_entity import WorkEntity


class TaskPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"


class TaskCadence(StrEnum):
    # A one-time task is done once and never expected to recur — the default, and
    # what most tasks actually are. daily/weekly/monthly DO regenerate, via
    # `worker/task_recurrence_scheduler.py`: for years these were labels the
    # system ignored, and 509 tasks carried one while somebody re-created the
    # work by hand every morning.
    ONE_TIME = "one_time"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


# Roles that count as "an admin" for the escalation badge - IT_ADMIN is a full
# admin org-wide (`CurrentUser._normalize_role`, `EmployeeRepository.list_admins`).
_ADMIN_ROLES = frozenset({Role.ADMIN, Role.IT_ADMIN})


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_assignee_status", "assignee_id", "status"),)

    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(String(2000), default=None)

    assignee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    # Who assigned it — nullable so soft-deleting an assigner never orphans rows.
    assigned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )

    # Free-text label (legacy) + an optional link to an admin-curated Project
    # (work_entities). SET NULL so deleting a project never deletes its tasks.
    project: Mapped[str | None] = mapped_column(String(128), default=None)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("work_entities.id", ondelete="SET NULL"), default=None, index=True
    )
    # The linked Project, selectin-loaded so a task read carries its project's
    # display name without an N+1. viewonly — the link is set via project_id.
    project_link: Mapped[WorkEntity | None] = relationship(
        "WorkEntity",
        primaryjoin="Task.project_id == WorkEntity.id",
        lazy="selectin",
        viewonly=True,
    )

    # The goal this work serves. SET NULL for the same reason as project_id:
    # retiring a target must not delete the tasks that were done towards it,
    # and the history of what was worked on is worth more than the link.
    #
    # One target per task, mirroring project_id rather than introducing a join
    # table. A task is a piece of work done towards a goal; if it genuinely
    # serves two, that is two tasks.
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("targets.id", ondelete="SET NULL"), default=None, index=True
    )
    # Selectin-loaded like project_link, so a list of tasks carries its targets'
    # titles without an N+1. viewonly - the link is set via target_id.
    target_link: Mapped[Target | None] = relationship(
        "Target",
        primaryjoin="Task.target_id == Target.id",
        lazy="selectin",
        viewonly=True,
    )

    @property
    def target_title(self) -> str | None:
        """The linked target's title, for the read model."""
        return self.target_link.title if self.target_link is not None else None
    priority: Mapped[TaskPriority] = mapped_column(default=TaskPriority.MEDIUM, index=True)
    status: Mapped[TaskStatus] = mapped_column(default=TaskStatus.TODO, index=True)
    cadence: Mapped[TaskCadence] = mapped_column(default=TaskCadence.ONE_TIME, index=True)

    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    remarks: Mapped[str | None] = mapped_column(String(2000), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Richer task fields (PMS spec): expected output, progress, review, blockers,
    # attachments ([{name,url}]), and links for weekly→daily breakdown + deps.
    expected_output: Mapped[str | None] = mapped_column(String(2000), default=None)
    completion_pct: Mapped[int] = mapped_column(default=0)
    review_notes: Mapped[str | None] = mapped_column(String(2000), default=None)
    final_outcome: Mapped[str | None] = mapped_column(String(2000), default=None)
    blocked_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    attachments: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # How far automatic overdue escalation has already gone, so each tier fires
    # exactly once however many times the sweep runs:
    #   0 = untouched · 1 = assignee warned · 2 = manager tier processed
    #   3 = admin tier processed
    # A tier is "processed" even when it found nobody to pull in, so the level
    # alone never says who is on the task - see `escalated_to` for that.
    # A bool could not express this: the sweep is idempotent by comparing the
    # level it is about to apply against the one already recorded.
    escalation_level: Mapped[int] = mapped_column(default=0, index=True)
    # A daily task can roll up to a weekly/monthly parent; deps gate sequencing.
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None, index=True
    )
    depends_on_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None, index=True
    )

    # --- recurrence ------------------------------------------------------- #
    # Every instance of one recurring task shares this. Deliberately NOT
    # `parent_task_id`, which already means sub-task: overloading it would
    # make "show me the sub-tasks" start returning last week's copy.
    #
    # SET NULL rather than CASCADE: deleting the first instance of a series
    # must not delete a year of completed work behind it.
    series_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None, index=True
    )
    # None = forever. A date stops the series without deleting its history,
    # which deleting the task would.
    recurs_until: Mapped[date | None] = mapped_column(Date, default=None)
    # Pause without losing the series. A task whose assignee is on leave for a
    # month should stop generating, not be recreated from scratch afterwards.
    recurrence_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Which playbook run stamped this task out, if any - see
    # `app/models/playbook.py`. The second way a task can arrive without
    # anybody typing it, alongside `series_id`, and the UI badges both.
    #
    # SET NULL, not CASCADE: cancelling a run deletes its tasks deliberately
    # and only while they are untouched. A task somebody has since worked on
    # must survive losing its provenance, not vanish with it.
    playbook_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("playbook_runs.id", ondelete="SET NULL"), default=None, index=True
    )

    # The THIRD way a task can arrive with nobody's name on it: the detector
    # saw sustained work on a project with no task behind it. A boolean, not a
    # foreign key, because the referent is the work entity and `project_id`
    # already carries it - see migration e4f6a8b0c2d5.
    auto_detected: Mapped[bool] = mapped_column(Boolean, default=False)

    # Collaborators get view + comment-level access (the task enters their scope
    # and they may report progress). Read-only on the Task — membership lives in
    # task_collaborators and is managed via the repository. selectin-loaded so a
    # task read carries its collaborator ids without an N+1 round-trip.
    collaborators: Mapped[list[Employee]] = relationship(
        secondary="task_collaborators",
        lazy="selectin",
        viewonly=True,
        order_by="Employee.full_name",
    )

    @property
    def collaborator_ids(self) -> list[uuid.UUID]:
        """The collaborator employee ids — surfaced on `TaskRead`."""
        return [collaborator.id for collaborator in self.collaborators]

    @property
    def escalated_to(self) -> str | None:
        """Who an escalation actually brought onto the task: "admin", "manager",
        or None. Surfaced on `TaskRead` for the board badge.

        Derived from the collaborators, never from `escalation_level` alone: the
        level advances even when a tier found nobody (no manager on file, no
        admin left to pick), so "level 3" used to badge "Escalated - admin" on
        tasks no admin was ever added to.
        """
        if not self.escalated:
            return None
        others = [c for c in self.collaborators if c.id != self.assignee_id]
        if self.escalation_level >= 3 and any(c.role in _ADMIN_ROLES for c in others):
            return "admin"
        if self.escalation_level >= 2 and others:
            return "manager"
        return None

    @property
    def project_name(self) -> str | None:
        """Display label surfaced on `TaskRead.project`: the linked Project's
        name when the task is connected to one, else the legacy free-text label."""
        if self.project_link is not None:
            return self.project_link.name
        return self.project

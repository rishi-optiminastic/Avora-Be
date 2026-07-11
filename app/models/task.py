"""Task — assigned work tracked against deadlines and projects.

A task is assigned BY one employee (a manager/admin, or the employee themselves)
TO an employee. Reads are scoped to the caller exactly like employees: you see
your own tasks, your reports'/department's tasks, or everything (admin/HR), plus
anything you assigned (Security rule 5.3 — scope enforced in the repository).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.employee import Employee
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
    # what most tasks actually are. daily/weekly/monthly are recurring cadences
    # (labels only; the system does not auto-regenerate them).
    ONE_TIME = "one_time"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


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
    # A daily task can roll up to a weekly/monthly parent; deps gate sequencing.
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None, index=True
    )
    depends_on_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None, index=True
    )

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
    def project_name(self) -> str | None:
        """Display label surfaced on `TaskRead.project`: the linked Project's
        name when the task is connected to one, else the legacy free-text label."""
        if self.project_link is not None:
            return self.project_link.name
        return self.project

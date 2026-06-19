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

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


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
    priority: Mapped[TaskPriority] = mapped_column(default=TaskPriority.MEDIUM, index=True)
    status: Mapped[TaskStatus] = mapped_column(default=TaskStatus.TODO, index=True)
    cadence: Mapped[TaskCadence] = mapped_column(default=TaskCadence.DAILY, index=True)

    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    remarks: Mapped[str | None] = mapped_column(String(2000), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

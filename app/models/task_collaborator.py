"""Task collaborators — a many-to-many between tasks and employees.

A collaborator is someone added to a task besides its assignee. They get
*view + comment-level* access: the task shows up in their scope (so they can
read it and its thread) and they may report progress on it (status etc.), but
the assignee/assigner of record never changes. Membership is its own table so
adding/removing a collaborator never touches the task row.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TaskCollaborator(Base):
    __tablename__ = "task_collaborators"

    # Composite primary key — one row per (task, employee); CASCADE so the link
    # disappears when either side is deleted.
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

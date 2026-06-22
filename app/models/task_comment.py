"""TaskComment — a message on a task's discussion thread.

Anyone who can see the task (same scope as the task itself — see
`TaskRepository._scope_clause`) may read and post. Authorization is enforced by
resolving the parent task through the scoped task repository before these rows
are ever touched. Mirrors `LeaveComment`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TaskComment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "task_comments"

    # One logical "send" carries one idempotency key. A replayed/copied request
    # carries the SAME key, so the unique (author, key) constraint turns the second
    # insert into a no-op and we return the original row — a looped curl can't post
    # duplicates. NULL for legacy rows / keyless callers (Postgres treats NULLs as
    # distinct, so those still insert freely).
    __table_args__ = (
        UniqueConstraint(
            "author_id", "idempotency_key", name="uq_task_comments_author_idempotency"
        ),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    # Author — SET NULL on offboard so the thread survives.
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    body: Mapped[str] = mapped_column(String(2000))
    idempotency_key: Mapped[str | None] = mapped_column(String(64), default=None)

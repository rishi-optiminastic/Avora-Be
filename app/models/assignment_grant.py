"""Assignment grants — an explicit "may assign work to" edge between employees.

The reporting tree answers most of "who may assign a task to whom": a manager
assigns to their direct reports, an admin/HR to anyone. But a real org has
authority that the tree can't express, because a person has exactly one manager
— e.g. two managers who both hand work to the same executive who reports to
neither of them.

A grant is that missing edge, stated explicitly rather than inferred: it lets
`assigner` create/reassign tasks for `assignee`, and nothing else. It does NOT
widen the employee read scope (Security rule 5.3) — the grantee gains no access
to that person's profile, monitoring, or attendance. Grants are admin/HR-managed
and additive; the tree still works on its own.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AssignmentGrant(Base):
    __tablename__ = "assignment_grants"

    # Composite primary key — one row per (assigner, assignee), so granting the
    # same pair twice is a no-op rather than a duplicate. CASCADE on both sides:
    # the edge is meaningless once either employee is gone.
    assigner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    # Which admin/HR granted it — nullable so removing them never drops the edge.
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

"""LeaveComment — a message on a leave request.

Anyone who can see the leave (the requester, their reviewer, HR/admin — same
scope as the leave itself) may read and post comments. Authorization is enforced
by resolving the parent leave through the scoped leave repository first.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LeaveComment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leave_comments"

    leave_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leaves.id", ondelete="CASCADE"), index=True
    )
    # Author — SET NULL on offboard so the thread survives.
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    body: Mapped[str] = mapped_column(String(2000))

"""Per-employee progress through the probation review checklist.

Only steps someone has actually acted on are stored. A missing row means the step
is still pending, so adding or reordering a step in `app.core.probation` never
needs a backfill and never invents history. `step_no` is the stable key from that
module, not a display position.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ProbationStepStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    # Steps like "collect feedback from the CEO, if applicable" do not always
    # apply. Marking them skipped keeps the record honest instead of forcing a
    # tick that says something happened when it did not.
    SKIPPED = "skipped"


class ProbationChecklistItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "probation_checklist_items"
    __table_args__ = (
        UniqueConstraint("employee_id", "step_no", name="uq_probation_item_employee_step"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    step_no: Mapped[int] = mapped_column()
    status: Mapped[ProbationStepStatus] = mapped_column(default=ProbationStepStatus.PENDING)
    note: Mapped[str | None] = mapped_column(String(1000), default=None)

    # Who last moved it, and when. SET NULL so offboarding a person never deletes
    # the review trail of someone they signed off.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

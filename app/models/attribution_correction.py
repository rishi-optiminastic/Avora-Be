"""Attribution correction — an employee re-labels their work to a project.

Auto-labelling (OCR → work_entities) is a best guess; the spec lets a user
correct it, "subject to manager review". A correction is the employee's claim
that their activity (optionally a specific day/screenshot) belongs to project P;
a manager in their scope approves or rejects it. Approved corrections are the
ground-truth label and can later feed the catalog (documented future).
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CorrectionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AttributionCorrection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attribution_corrections"
    __table_args__ = (Index("ix_attr_corrections_employee_status", "employee_id", "status"),)

    # Whose work this re-labels (always the proposer here — self-correction).
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    proposed_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE")
    )
    # The project it should be (NULL = "none / not work").
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("work_entities.id", ondelete="SET NULL"), default=None
    )
    # Optional anchors narrowing what the correction applies to.
    screenshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("screenshots.id", ondelete="SET NULL"), default=None
    )
    for_date: Mapped[str | None] = mapped_column(String(10), default=None)  # YYYY-MM-DD

    status: Mapped[CorrectionStatus] = mapped_column(default=CorrectionStatus.PENDING, index=True)
    note: Mapped[str | None] = mapped_column(String(500), default=None)
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    review_note: Mapped[str | None] = mapped_column(String(500), default=None)

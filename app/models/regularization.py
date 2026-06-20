"""Regularization — a request to count a late day as a full day.

An employee requests it for a specific (local) date; a manager/HR approves,
which spends one of the employee's monthly regularization credits and makes that
day classify as FULL_DAY. `day` is the local office date as YYYY-MM-DD.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RegularizationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Regularization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "regularizations"
    __table_args__ = (Index("ix_regularizations_employee_day", "employee_id", "day"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (local office date)
    reason: Mapped[str | None] = mapped_column(String(500), default=None)
    status: Mapped[RegularizationStatus] = mapped_column(
        default=RegularizationStatus.PENDING, index=True
    )
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    review_note: Mapped[str | None] = mapped_column(String(500), default=None)

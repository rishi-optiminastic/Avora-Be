"""Resignation — an employee's notice, decided by HR/Admin.

An employee submits a resignation (reason + intended last working day). HR/Admin
accept or reject it (segregation of duties — never your own). Accepting does NOT
deactivate the account: offboarding stays a separate, deliberate HR action.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ResignationStatus(StrEnum):
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class Resignation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "resignations"
    __table_args__ = (Index("ix_resignations_employee_status", "employee_id", "status"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str | None] = mapped_column(String(2000), default=None)
    last_working_day: Mapped[date] = mapped_column(Date)

    status: Mapped[ResignationStatus] = mapped_column(
        default=ResignationStatus.SUBMITTED, index=True
    )
    # Who decided (HR/Admin) — nullable until decided, SET NULL on offboard.
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    decision_note: Mapped[str | None] = mapped_column(String(1000), default=None)

"""The outcome of a probation review: confirmed, extended, or terminated.

One row per employee — a review reaches a single conclusion, and re-deciding
replaces it rather than stacking. `effective_date` reads differently per outcome
and the schema layer says which is required:

  CONFIRMED  — the date the confirmation takes effect
  EXTENDED   — the new probation end date
  TERMINATED — the last working day

An extension also keeps what its letter needs: the end date it replaced
(`original_end_date`) and the numbered expectations HR set (`expectations`).
The letter itself is not stored - it is re-rendered from these on download.

Recording a decision is separate from emailing it: `letter_sent_at` is stamped
only when the letter actually goes out, so a send that fails is visible rather
than assumed.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import JSON, Date, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ProbationOutcome(StrEnum):
    CONFIRMED = "confirmed"
    EXTENDED = "extended"
    TERMINATED = "terminated"


class ProbationDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "probation_decisions"
    __table_args__ = (UniqueConstraint("employee_id", name="uq_probation_decisions_employee_id"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    outcome: Mapped[ProbationOutcome] = mapped_column()
    effective_date: Mapped[date] = mapped_column(Date)
    # Snapshotted at decision time: the confirmation letter names the title the
    # person was confirmed INTO, which a later promotion must not rewrite.
    job_title: Mapped[str | None] = mapped_column(String(128), default=None)
    note: Mapped[str | None] = mapped_column(String(1000), default=None)
    # EXTENDED only: the end date before the (first) extension, which the letter
    # quotes as "originally set to end on".
    original_end_date: Mapped[date | None] = mapped_column(Date, default=None)
    # EXTENDED only: HR's 1-5 expectations, validated at the schema layer.
    expectations: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    letter_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

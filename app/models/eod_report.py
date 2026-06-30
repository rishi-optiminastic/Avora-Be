"""EodReport — one employee's End-of-Day summary for a local calendar day.

Generated from the day's tasks + activity + screenshot OCR text by the LLM, then
reviewed/approved by the employee before it emails their manager + admins. One
row per (employee, report_date) — the unique constraint is the idempotency guard
the scheduler relies on (mirrors `payroll_runs.period_month`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EodStatus(StrEnum):
    DRAFT = "draft"  # generated, awaiting the employee's review
    APPROVED = "approved"  # employee approved (transient, before send)
    SENT = "sent"  # emailed to manager + admins
    SKIPPED_ABSENT = "skipped_absent"  # employee was absent — no report
    FAILED = "failed"  # generation errored (recorded, not retried blindly)


class EodReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "eod_reports"
    __table_args__ = (
        UniqueConstraint("employee_id", "report_date", name="uq_eod_reports_employee_report_date"),
        # The overdue-draft sweep filters status + created_at; a composite serves
        # that (and status-alone queries via the leading column).
        Index("ix_eod_reports_status_created", "status", "created_at"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    report_date: Mapped[str] = mapped_column(String(10), index=True)  # local YYYY-MM-DD
    status: Mapped[EodStatus] = mapped_column(default=EodStatus.DRAFT)

    # The generated narrative; `edited_summary` holds the employee's override.
    summary: Mapped[str] = mapped_column(Text, default="")
    edited_summary: Mapped[str | None] = mapped_column(Text, default=None)
    highlights: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Day metrics shown on the email's stat card (worked_minutes, active_minutes,
    # tasks_done) — captured at generation so the send path never recomputes.
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    error: Mapped[str | None] = mapped_column(String(512), default=None)

    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(default=None)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

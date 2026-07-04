"""End-of-Day report schemas (Golden rule #5 — never return the ORM model).

`EodDraftContent` is the strict JSON shape the LLM must return. `EodReportRead`
is the API view (effective summary = the employee's edit if present, else the
generated draft). `EodReportUpdate` is the employee's edit before approval.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from app.models.eod_report import EodReport


class EodDraftContent(BaseModel):
    """The LLM's structured output for one employee's day."""

    summary: str = ""
    worked_on: list[str] = Field(default_factory=list)
    tasks_completed: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    confidence: int = 0


class EodReportUpdate(BaseModel):
    """Employee edit of the draft narrative before approval."""

    summary: str = Field(min_length=1, max_length=10000)


class EodReportRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    report_date: str
    status: str
    summary: str
    highlights: EodDraftContent
    created_at: datetime
    sent_at: datetime | None

    @classmethod
    def from_model(cls, report: EodReport) -> EodReportRead:
        highlights = EodDraftContent.model_validate(report.highlights or {})
        return cls(
            id=report.id,
            employee_id=report.employee_id,
            report_date=report.report_date,
            status=report.status.value,
            # The employee's edit wins over the generated draft.
            summary=report.edited_summary or report.summary,
            highlights=highlights,
            created_at=report.created_at,
            sent_at=report.sent_at,
        )


class EodCumulativeTotals(BaseModel):
    """Summed effort across every content report in the digest window."""

    worked_minutes: int = 0
    active_minutes: int = 0
    tasks_done: int = 0
    blockers: int = 0


class EodCumulativeMember(BaseModel):
    """One person's coverage in the digest window — the roster row that shows who
    submitted, who's still pending, who was absent, and who's missing entirely."""

    employee_id: uuid.UUID
    name: str
    department: str | None = None
    job_title: str | None = None
    # submitted | pending | absent | failed | missing
    coverage: str
    latest_report_id: uuid.UUID | None = None
    latest_report_date: str | None = None


class EodCumulativeRead(BaseModel):
    """A rolled-up team digest over [from_date, to_date]: coverage counts, summed
    effort, the per-person roster, and every report that carries content."""

    from_date: str
    to_date: str
    member_count: int
    submitted: int
    pending: int
    absent: int
    failed: int
    missing: int
    submission_rate: float  # submitted / member_count, 0..1
    totals: EodCumulativeTotals
    reports: list[EodReportRead]
    members: list[EodCumulativeMember]

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
    submitted_days: int = 0  # distinct expected days reported in the window
    latest_report_id: uuid.UUID | None = None
    latest_report_date: str | None = None


class EodRollup(BaseModel):
    """Coverage + summed effort over [from_date, to_date] — the shape every EOD
    rollup shares, whether it covers a team or a department. Fields stay required
    so a caller that forgets one fails loudly instead of reporting a quiet zero."""

    from_date: str
    to_date: str
    member_count: int
    submitted: int
    pending: int
    absent: int
    failed: int
    missing: int
    # Per day, not per person: submitted_days / (member_count * working_days),
    # 0..1. The five counts above are one representative band per person, so on
    # a multi-day window they say who reported at all, not how often.
    submission_rate: float
    working_days: int  # days in the window a report was due (work week - holidays)
    submitted_days: int  # distinct (person, expected day) pairs reported
    totals: EodCumulativeTotals


class EodCumulativeRead(EodRollup):
    """A rolled-up team digest: the shared coverage/effort rollup plus the
    per-person roster and every report that carries content. This one DOES carry
    the narratives, so it is only ever served through the personal scope — a
    manager reading their own reports."""

    reports: list[EodReportRead]
    members: list[EodCumulativeMember]


class EodDepartmentMember(BaseModel):
    """One person's line in a department rollup.

    Carries the day's numbers and the work shipped, but deliberately NO
    `summary`. That line matters: a completed task is an organisational fact
    that already sits on the board for anyone in scope to see, whereas the
    end-of-day narrative is the person's own account of their day and goes to
    their reporting manager alone. Adding a `summary` field here would quietly
    undo that - don't.
    """

    employee_id: uuid.UUID
    name: str
    job_title: str | None = None
    # submitted | pending | absent | failed | missing
    coverage: str
    submitted_days: int = 0  # distinct expected days reported in the window
    worked_minutes: int = 0
    active_minutes: int = 0
    active_pct: int = 0  # active/worked, rounded; 0 when nothing was worked
    tasks_done: int = 0
    blockers: int = 0
    # Titles of what they actually shipped, in report order. `tasks_done` can
    # exceed len(tasks) - the count comes from the day's metrics, while these are
    # the titles the report happened to name.
    tasks: list[str] = Field(default_factory=list)
    # What is still on their plate RIGHT NOW, from the task board rather than the
    # report: `tasks_done` is what the day closed, these are what it left open.
    # Counts only - never titles, so this cannot leak a task out of scope.
    tasks_open: int = 0
    tasks_overdue: int = 0


class EodDepartmentDigest(EodRollup):
    """One department's rolled-up day: coverage, summed effort, the roster, and
    the blockers the department raised.

    `blockers` is intentionally unattributed — "what is holding this department
    up" is operational, where "what X is stuck on" is personal.
    """

    department: str  # the department name, or the unassigned-bucket label
    members: list[EodDepartmentMember]
    blockers: list[str]
    # The department's standing workload, summed off the members: what is open
    # across the team and how much of it has slipped its due date.
    open_tasks: int = 0
    overdue_tasks: int = 0

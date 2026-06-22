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

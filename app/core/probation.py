"""The probation review checklist — the org's 16-step process, as written.

The steps are policy, not data: they describe how a confirmation decision gets
made and who is accountable at each point. They live here as a constant so the
sequence can't drift per employee, and so the API, the UI and the tests all read
the same list.

`no` is a STABLE key, not a display position. The original process had a step 11
("Schedule Probation Meeting…") that was struck out and folded into step 12, so
11 is absent and the remaining numbers keep the values HR already uses in their
own sheet. Display order is the order of this tuple; `position` is derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class StepOwner(StrEnum):
    """Who is accountable for a step. Drives who is allowed to tick it off."""

    HR = "hr"
    EMPLOYEE = "employee"
    MANAGER = "manager"
    HR_AND_MANAGER = "hr_and_manager"


@dataclass(frozen=True, slots=True)
class ProbationStep:
    no: int
    title: str
    owner: StepOwner
    timeline: str
    # Sub-points the sheet carries as extra lines inside the same cell.
    detail: tuple[str, ...] = ()


_BEFORE_MEETING = "Before review meeting"
_BEFORE_END = "Before probation end date"
_THREE_WEEKS = "3 weeks (21 days) before probation end date"

STEPS: tuple[ProbationStep, ...] = (
    ProbationStep(1, "Initiate probation review process", StepOwner.HR, _THREE_WEEKS),
    ProbationStep(2, "Share employee self-assessment form", StepOwner.HR, _THREE_WEEKS),
    ProbationStep(
        3,
        "Employee completes self-assessment",
        StepOwner.EMPLOYEE,
        "Before manager review",
        ("Covering achievements, challenges and areas of improvement",),
    ),
    ProbationStep(4, "Share probation form to reporting manager", StepOwner.HR, ""),
    ProbationStep(
        5,
        "Reporting manager reviews performance",
        StepOwner.MANAGER,
        _BEFORE_MEETING,
        ("Against role expectations, KPIs, deliverables and objectives",),
    ),
    ProbationStep(
        6,
        "Reporting manager completes probation evaluation / performance matrix",
        StepOwner.MANAGER,
        _BEFORE_MEETING,
    ),
    ProbationStep(
        7,
        "Finalise confirmation / extension / termination decision",
        StepOwner.HR_AND_MANAGER,
        _BEFORE_END,
    ),
    ProbationStep(
        8, "HR reviews attendance and punctuality records", StepOwner.HR, _BEFORE_MEETING
    ),
    ProbationStep(
        9, "HR reviews adherence to company policies and conduct", StepOwner.HR, _BEFORE_MEETING
    ),
    ProbationStep(
        10,
        "Collect additional feedback from relevant team members / CEO, if applicable",
        StepOwner.HR_AND_MANAGER,
        _BEFORE_MEETING,
    ),
    # 11 is deliberately absent — folded into 12 when the process was revised.
    ProbationStep(
        12,
        "Schedule and conduct the probation review meeting",
        StepOwner.HR,
        _BEFORE_END,
        (
            "With the employee, reporting manager and HR",
            "Discuss performance, achievements, gaps and future expectations",
            "Employee shares feedback, concerns and future goals",
        ),
    ),
    ProbationStep(
        13,
        "Prepare the relevant letter / documentation",
        StepOwner.HR,
        "After decision",
        ("Confirmation", "Extension", "Termination of employment"),
    ),
    ProbationStep(
        14,
        "Employee acknowledges and signs the confirmation / extension documentation",
        StepOwner.EMPLOYEE,
        "As per HR timeline",
    ),
    ProbationStep(15, "Update HRMS (Avora)", StepOwner.HR, "After documentation"),
    ProbationStep(16, "Update Insurance GoDigit Portal", StepOwner.HR, "After confirmation"),
)

STEPS_BY_NO: dict[int, ProbationStep] = {step.no: step for step in STEPS}

# How many days before the probation end date the process is meant to start.
REVIEW_LEAD_DAYS = 21

# How long a finished review stays on the roster after the person is confirmed.
# Without this a completed 16-step review becomes unreachable the very next day:
# the record is still in the database but nothing in the UI links to it.
RECENT_CONFIRMATION_DAYS = 60


def step_or_none(no: int) -> ProbationStep | None:
    return STEPS_BY_NO.get(no)

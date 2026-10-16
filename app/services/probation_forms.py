"""The probation review forms: when they go out, and how they move the
probation checklist.

Three forms, all opening `trigger_days` (default 21, `REVIEW_LEAD_DAYS`)
before a probationer's probation end date:

- PROBATION_SELF to the probationer.
- PROBATION_MANAGER to their current reporting manager, about them. With no
  manager on file it waits as NEEDS_MANAGER (HR is told once) and goes out
  when one is set.
- PROBATION_HR to the HR pool: one shared assignment per probationer that any
  HR or admin may fill in; whoever submits first claims it.

Who is due: an active employee with a joining date, still on probation, whose
review window has opened (org-today on or after end - trigger_days, and before
end). The probation end is computed the same way as everywhere else
(`core.tenure.probation_end`, the person's own months or the org default), so
an extension simply moves the window. People whose probation already ended
are never sent the forms, so publishing a form never mails old staff.

`Form.auto_assign_since` is compared to the probation END date: only
probations ending after the form went live are reviewed. Together with "still
on probation" that is always true for anyone due today, so publishing reaches
exactly the people currently in (or later entering) their review window,
including those already inside it.

One assignment per (trigger, probationer) ever, like the joiner forms: an
extended probation is not sent a second round automatically (HR can send the
forms by hand).

The checklist is ticked by Avora as a clear system actor (no person, a note
starting "Done in Avora:"), and never over a step a person already settled.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.tenure import probation_end
from app.models.employee import Employee
from app.models.form import AssignmentStatus, Form, FormAssignment, FormTrigger
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.leave_policy import LeavePolicyRepository
from app.repositories.probation_checklist import ProbationChecklistRepository
from app.schemas.form import PROBATION_TRIGGERS, SubjectDetails

# Checklist steps (stable `no` keys from app.core.probation) and the note each
# automatic tick leaves.
_STARTED = {1: "probation review forms sent."}
_ON_OPEN: dict[FormTrigger, dict[int, str]] = {
    FormTrigger.PROBATION_SELF: {2: "self-assessment form sent to the employee."},
    FormTrigger.PROBATION_MANAGER: {4: "probation evaluation form sent to the reporting manager."},
    FormTrigger.PROBATION_HR: {},
}
_ON_SUBMIT: dict[FormTrigger, dict[int, str]] = {
    FormTrigger.PROBATION_SELF: {3: "self-assessment form submitted."},
    FormTrigger.PROBATION_MANAGER: {
        5: "reporting manager submitted the probation evaluation.",
        6: "reporting manager submitted the probation evaluation.",
    },
    FormTrigger.PROBATION_HR: {
        8: "HR probation review form submitted.",
        9: "HR probation review form submitted.",
    },
}


# --- pure rules (unit-tested directly) ------------------------------------ #
def is_probation_form(trigger: FormTrigger) -> bool:
    return trigger in PROBATION_TRIGGERS


def review_opens_on(ends_on: date, days: int | None) -> date:
    return ends_on - timedelta(days=days or 0)


def is_review_due(employee: Employee, ends_on: date | None, form: Form, today: date) -> bool:
    """Send this probation form for this person today?"""
    if ends_on is None or form.auto_assign_since is None or not employee.is_active:
        return False
    if ends_on <= form.auto_assign_since:
        return False  # their probation ended before the form went live
    return review_opens_on(ends_on, form.trigger_days) <= today < ends_on


def usable_manager(subject: Employee | None, manager: Employee | None) -> bool:
    return (
        subject is not None
        and manager is not None
        and manager.is_active
        and manager.id != subject.id
    )


def is_pool(assignment: FormAssignment) -> bool:
    """The shared HR form, not yet claimed by anyone."""
    return assignment.trigger is FormTrigger.PROBATION_HR and assignment.assignee_id is None


def probation_values(
    form: Form, subject: Employee, manager: Employee | None, ends_on: date, today: date
) -> dict[str, object]:
    """The new assignment for this probationer. Due within `due_days`, but
    never after the probation end date."""
    due_on = min(today + timedelta(days=form.due_days), ends_on - timedelta(days=1))
    values: dict[str, object] = {
        "form_id": form.id,
        "subject_id": subject.id,
        "trigger": form.trigger,
        "available_on": min(today, review_opens_on(ends_on, form.trigger_days)),
        "due_on": max(due_on, today),
        "status": AssignmentStatus.PENDING,
    }
    if form.trigger is FormTrigger.PROBATION_SELF:
        values["assignee_id"] = subject.id
    elif form.trigger is FormTrigger.PROBATION_MANAGER:
        if usable_manager(subject, manager):
            values["assignee_id"] = subject.manager_id
        else:
            values["status"] = AssignmentStatus.NEEDS_MANAGER
    return values  # PROBATION_HR: nobody yet, the HR pool


class ProbationFormLink:
    """What the forms services need to know about probation: the org's clock,
    end dates, the subject facts shown on the form, and the checklist."""

    def __init__(
        self,
        checklist: ProbationChecklistRepository,
        leave_policy: LeavePolicyRepository,
        attendance_policy: AttendancePolicyRepository,
        employees: EmployeeRepository,
    ) -> None:
        self._checklist = checklist
        self._leave_policy = leave_policy
        self._attendance_policy = attendance_policy
        self._employees = employees

    async def org_today(self) -> date:
        """Today in the org's timezone, never the server's clock."""
        policy = await self._attendance_policy.get()
        zone = ZoneInfo(policy.timezone) if policy is not None else UTC
        return datetime.now(UTC).astimezone(zone).date()

    async def end_dates(self, people: Iterable[Employee]) -> dict[uuid.UUID, date]:
        """Probation end date per person with a joining date on record."""
        policy = await self._leave_policy.get()
        default_months = int(policy.probation_months) if policy is not None else _DEFAULT_MONTHS
        return {
            p.id: probation_end(p.hire_date, p.probation_months or default_months)
            for p in people
            if p.hire_date is not None
        }

    async def subject_details(
        self, subject_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, SubjectDetails]:
        """Read-only facts for the form header, keyed by subject. The caller
        has already been allowed to open the form these belong to."""
        subjects = await self._employees.get_many(list(dict.fromkeys(subject_ids)))
        managers = await self._employees.get_many(
            [s.manager_id for s in subjects.values() if s.manager_id is not None]
        )
        ends = await self.end_dates(subjects.values())
        return {
            s.id: SubjectDetails(
                employee_name=s.full_name,
                department=s.department,
                designation=s.job_title,
                reporting_manager=(
                    managers[s.manager_id].full_name if s.manager_id in managers else None
                ),
                date_of_joining=s.hire_date,
                probation_end_date=ends.get(s.id),
            )
            for s in subjects.values()
        }

    async def on_opened(self, assignment: FormAssignment) -> None:
        """A probation form went out (or is waiting for a manager)."""
        if assignment.subject_id is None or not is_probation_form(assignment.trigger):
            return
        steps = dict(_STARTED)
        if assignment.status is AssignmentStatus.PENDING:
            steps.update(_ON_OPEN[assignment.trigger])
        await self._tick(assignment.subject_id, steps)

    async def on_submitted(self, assignment: FormAssignment) -> None:
        if assignment.subject_id is None or not is_probation_form(assignment.trigger):
            return
        await self._tick(assignment.subject_id, _ON_SUBMIT[assignment.trigger])

    async def _tick(self, employee_id: uuid.UUID, steps: dict[int, str]) -> None:
        await self._checklist.mark_done_by_system(employee_id, steps, datetime.now(UTC))


# Matches LeavePolicy.probation_months' own default, for an org with no policy row.
_DEFAULT_MONTHS = 6

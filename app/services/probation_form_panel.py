"""The probation review forms as seen from one probationer's checklist page.

Anyone who may see the checklist (HR/admin, the senior manager or manager in
scope, the probationer) sees where each of the three forms stands. Answers
are another matter: only HR and admins may open them, plus each assignee
their own submission. The weighted score is part of the answers, so it
follows the same rule.
"""

from __future__ import annotations

import uuid

from app.core.exceptions import NotFoundError
from app.models.form import AssignmentStatus, FormAssignment, FormTrigger
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.form import (
    AssignmentRow,
    FormAssignmentRepository,
    FormRepository,
    FormResponseRepository,
)
from app.schemas.auth import CurrentUser
from app.schemas.form import ProbationForms, ProbationFormStatus
from app.services.form_scoring import field_weights, score_answers
from app.services.form_views import can_manage_forms, to_answer

# Display order on the checklist page.
_ORDER = (FormTrigger.PROBATION_SELF, FormTrigger.PROBATION_MANAGER, FormTrigger.PROBATION_HR)
# Triggers that no longer go out on their own. Kept in `_ORDER` so a review
# sent before the merge still shows its history.
_MERGED_AWAY = frozenset({FormTrigger.PROBATION_MANAGER})


class ProbationFormPanelService:
    def __init__(
        self,
        forms: FormRepository,
        assignments: FormAssignmentRepository,
        responses: FormResponseRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._forms = forms
        self._assignments = assignments
        self._responses = responses
        self._employees = employees
        self._audit = audit

    async def for_employee(self, caller: CurrentUser, employee_id: uuid.UUID) -> ProbationForms:
        # The same scope as the checklist itself, which means the subject is
        # refused too. `_may_open_answers` already kept the ANSWERS and the
        # score from them, but the panel still reported each form's status,
        # who was reviewing them and when it came back - the machinery of their
        # own assessment, which is exactly what the checklist stopped showing.
        # 404 so existence never leaks.
        if employee_id == caller.employee_id:
            raise NotFoundError()
        if not await self._employees.can_read(caller, employee_id):
            raise NotFoundError()
        rows = await self._assignments.for_subject(employee_id, _ORDER)
        by_trigger = {
            r[0].trigger: r for r in rows if r[0].status is not AssignmentStatus.CANCELLED
        }
        statuses = [await self._status(caller, t, by_trigger.get(t)) for t in _ORDER]
        # The manager evaluation was folded into the employee's form as its
        # second stage, so there is no longer a separate one to publish or
        # send. Its empty row is work that no longer exists, and listing it is
        # what made this page describe a sequence it was not running.
        #
        # Only this trigger, and only when it is truly empty: an unpublished
        # HR form still earns its row, because "nobody has published it yet"
        # is exactly what HR needs to see. A probationer reviewed BEFORE the
        # merge keeps their row too, because the assignment is still there.
        forms = [
            f
            for f in statuses
            if f.trigger not in _MERGED_AWAY or f.assignment_id is not None or f.form_title
        ]
        await self._audit.append(
            actor=str(caller.employee_id),
            action="probation.forms.read",
            target=f"employee:{employee_id}",
        )
        return ProbationForms(employee_id=employee_id, forms=forms)

    async def _status(
        self, caller: CurrentUser, trigger: FormTrigger, row: AssignmentRow | None
    ) -> ProbationFormStatus:
        if row is None:
            live = await self._forms.published_for_trigger(trigger)
            return ProbationFormStatus(trigger=trigger, form_title=live[0].title if live else None)
        assignment, form_title, assignee_name, _ = row
        can_open = await self._may_open_answers(caller, assignment)
        return ProbationFormStatus(
            trigger=trigger,
            form_title=form_title,
            assignment_id=assignment.id,
            status=assignment.status,
            assignee_name=assignee_name,
            available_on=assignment.available_on,
            due_on=assignment.due_on,
            submitted_at=assignment.submitted_at,
            weighted_total=await self._total(assignment) if can_open else None,
            can_open=can_open,
        )

    async def _may_open_answers(self, caller: CurrentUser, assignment: FormAssignment) -> bool:
        """Whether the link on the checklist actually opens.

        This MUST agree with `FormAssignmentService.detail`, or the panel
        offers a link that 404s - which is how a checklist starts lying about
        what you can do. The reporting manager holds the form outright once it
        has been handed to them; the line above them reads it as oversight.
        """
        if _may_open_answers(caller, assignment):
            return True
        if assignment.subject_id is None or assignment.subject_id == caller.employee_id:
            return False
        if assignment.status not in (AssignmentStatus.PENDING, AssignmentStatus.SUBMITTED):
            return False
        return await self._employees.can_read(caller, assignment.subject_id)

    async def _total(self, assignment: FormAssignment) -> float | None:
        """The weighted score out of 100, once submitted."""
        if assignment.status is not AssignmentStatus.SUBMITTED:
            return None
        response = (await self._responses.for_assignments([assignment.id])).get(assignment.id)
        if response is None:
            return None
        weights = field_weights(await self._forms.fields_of(assignment.form_id))
        answers = (await self._responses.answers_for([response.id])).get(response.id, [])
        return score_answers(weights, [to_answer(a) for a in answers])[1]


def _may_open_answers(caller: CurrentUser, assignment: FormAssignment) -> bool:
    """The part of the rule that needs no database: the assignee (to fill it
    in, or read their own answers), or HR/admin, never about themselves."""
    if assignment.status not in (AssignmentStatus.PENDING, AssignmentStatus.SUBMITTED):
        return False
    if assignment.assignee_id == caller.employee_id:
        return True
    return can_manage_forms(caller) and assignment.subject_id != caller.employee_id

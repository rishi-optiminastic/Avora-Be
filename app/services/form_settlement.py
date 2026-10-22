"""Is an assignment nobody has been told about yet still right to send?

Checked by the daily sweep and again right before each email, because time
passes between an assignment being made and its email going out: the joiner
may leave, the buddy may leave or change, a probationer's manager may change,
HR may withdraw the form or change who it goes to. Pure, so every case is unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.models.employee import Employee
from app.models.form import AssignmentStatus, FormAssignment, FormStage, FormTrigger
from app.services.probation_forms import is_pool, usable_manager


class Settle(StrEnum):
    KEEP = "keep"
    CANCEL = "cancel"
    # Send it to the joiner's current buddy.
    TO_BUDDY = "to_buddy"
    # Hold it until a buddy is on file (and tell HR).
    WAIT_FOR_BUDDY = "wait_for_buddy"
    # Send it to the probationer's current reporting manager.
    TO_MANAGER = "to_manager"
    # Hold it until a manager is on file (and tell HR).
    WAIT_FOR_MANAGER = "wait_for_manager"


@dataclass(frozen=True)
class Situation:
    # The trigger the form's live version sends by, or None if withdrawn.
    live_trigger: FormTrigger | None
    joiner: Employee | None
    assignee: Employee | None
    buddy: Employee | None
    # The subject's current reporting manager (probation manager forms).
    manager: Employee | None = None


def is_unsent(assignment: FormAssignment) -> bool:
    if assignment.status in (AssignmentStatus.NEEDS_BUDDY, AssignmentStatus.NEEDS_MANAGER):
        return True
    return assignment.status is AssignmentStatus.PENDING and assignment.notified_at is None


def is_handed_over(assignment: FormAssignment) -> bool:
    """A two-stage form that has moved to its manager half.

    `handed_over_at` is the proof somebody already wrote the first half, which
    is what makes cancelling it destructive rather than tidy.
    """
    return assignment.stage is FormStage.MANAGER and assignment.handed_over_at is not None


def usable_buddy(joiner: Employee | None, buddy: Employee | None) -> bool:
    return joiner is not None and buddy is not None and buddy.is_active and buddy.id != joiner.id


def settle(assignment: FormAssignment, now: Situation) -> Settle:
    automatic = assignment.trigger is not FormTrigger.MANUAL
    if automatic and now.live_trigger is not assignment.trigger:
        return Settle.CANCEL  # withdrawn, or it no longer goes out this way
    if assignment.subject_id is not None and (now.joiner is None or not now.joiner.is_active):
        return Settle.CANCEL  # the person it is about has left
    if assignment.trigger is FormTrigger.BUDDY:
        return _settle_buddy(assignment, now)
    if assignment.trigger is FormTrigger.PROBATION_MANAGER:
        return _settle_manager(assignment, now)
    if is_handed_over(assignment):
        # A two-stage form whose first half is already written. Without this it
        # falls to the "no assignee left -> CANCEL" rule below and the sweep
        # destroys the employee's submitted answers overnight.
        return _settle_manager(assignment, now)
    if is_pool(assignment):
        return Settle.KEEP  # the HR pool: nobody in particular fills it in yet
    if now.assignee is None or not now.assignee.is_active:
        return Settle.CANCEL  # nobody left to fill it in
    return Settle.KEEP


def _settle_buddy(assignment: FormAssignment, now: Situation) -> Settle:
    has_buddy = usable_buddy(now.joiner, now.buddy)
    if assignment.status is AssignmentStatus.NEEDS_BUDDY:
        return Settle.TO_BUDDY if has_buddy else Settle.KEEP
    if now.assignee is not None and now.assignee.is_active:
        return Settle.KEEP
    return Settle.TO_BUDDY if has_buddy else Settle.WAIT_FOR_BUDDY


def _settle_manager(assignment: FormAssignment, now: Situation) -> Settle:
    """Unlike a buddy, the manager form always follows the CURRENT manager: if
    the reporting line changed before anyone was told, the new manager gets it."""
    has_manager = usable_manager(now.joiner, now.manager)
    if assignment.status is AssignmentStatus.NEEDS_MANAGER:
        return Settle.TO_MANAGER if has_manager else Settle.KEEP
    if has_manager and now.manager is not None and assignment.assignee_id == now.manager.id:
        return Settle.KEEP
    return Settle.TO_MANAGER if has_manager else Settle.WAIT_FOR_MANAGER

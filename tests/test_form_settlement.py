"""Is an unsent assignment still right to send? Every rule of `settle`."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.models.employee import Employee
from app.models.form import AssignmentStatus, FormAssignment, FormTrigger
from app.services.form_settlement import Settle, Situation, is_unsent, settle


def _person(*, active: bool = True, buddy: Employee | None = None) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        is_active=active,
        buddy_id=buddy.id if buddy else None,
        work_email="x@corp.test",
        full_name="X",
    )


def _assignment(
    trigger: FormTrigger,
    status: AssignmentStatus = AssignmentStatus.PENDING,
    *,
    joiner: Employee | None = None,
    assignee: Employee | None = None,
) -> FormAssignment:
    return FormAssignment(
        id=uuid.uuid4(),
        form_id=uuid.uuid4(),
        trigger=trigger,
        status=status,
        subject_id=joiner.id if joiner else None,
        assignee_id=assignee.id if assignee else None,
        available_on=date(2026, 11, 1),
    )


def _now(
    live: FormTrigger | None,
    joiner: Employee | None = None,
    assignee: Employee | None = None,
    buddy: Employee | None = None,
) -> Situation:
    return Situation(live_trigger=live, joiner=joiner, assignee=assignee, buddy=buddy)


@pytest.mark.parametrize("live", [None, FormTrigger.BUDDY, FormTrigger.MANUAL])
def test_withdrawn_or_retargeted_automatic_form_is_cancelled(live: FormTrigger | None) -> None:
    joiner = _person()
    a = _assignment(FormTrigger.NEW_JOINER, joiner=joiner, assignee=joiner)
    assert settle(a, _now(live, joiner, joiner)) is Settle.CANCEL


def test_joiner_who_left_cancels_both_forms() -> None:
    gone, buddy = _person(active=False), _person()
    mine = _assignment(FormTrigger.NEW_JOINER, joiner=gone, assignee=gone)
    theirs = _assignment(FormTrigger.BUDDY, joiner=gone, assignee=buddy)
    assert settle(mine, _now(FormTrigger.NEW_JOINER, gone, gone)) is Settle.CANCEL
    assert settle(theirs, _now(FormTrigger.BUDDY, gone, buddy, buddy)) is Settle.CANCEL


def test_waiting_buddy_form_goes_to_a_new_buddy() -> None:
    buddy = _person()
    joiner = _person(buddy=buddy)
    a = _assignment(FormTrigger.BUDDY, AssignmentStatus.NEEDS_BUDDY, joiner=joiner)
    assert settle(a, _now(FormTrigger.BUDDY, joiner, None, buddy)) is Settle.TO_BUDDY
    assert settle(a, _now(FormTrigger.BUDDY, joiner, None, None)) is Settle.KEEP


def test_buddy_who_left_is_replaced_or_the_form_waits() -> None:
    old = _person(active=False)
    new = _person()
    joiner = _person(buddy=new)
    a = _assignment(FormTrigger.BUDDY, joiner=joiner, assignee=old)
    assert settle(a, _now(FormTrigger.BUDDY, joiner, old, new)) is Settle.TO_BUDDY
    assert settle(a, _now(FormTrigger.BUDDY, joiner, old, None)) is Settle.WAIT_FOR_BUDDY
    # The joiner can't be their own buddy.
    assert settle(a, _now(FormTrigger.BUDDY, joiner, old, joiner)) is Settle.WAIT_FOR_BUDDY


def test_manual_form_to_someone_who_left_is_cancelled() -> None:
    gone = _person(active=False)
    a = _assignment(FormTrigger.MANUAL, assignee=gone)
    # A manual form doesn't depend on a live trigger.
    assert settle(a, _now(None, None, gone)) is Settle.CANCEL
    here = _person()
    assert settle(_assignment(FormTrigger.MANUAL, assignee=here), _now(None, None, here)) is (
        Settle.KEEP
    )


def test_only_untold_work_is_unsent() -> None:
    a = _assignment(FormTrigger.MANUAL)
    assert is_unsent(a)
    a.notified_at = datetime(2026, 11, 1, tzinfo=UTC)
    assert not is_unsent(a)
    assert is_unsent(_assignment(FormTrigger.BUDDY, AssignmentStatus.NEEDS_BUDDY))
    assert not is_unsent(_assignment(FormTrigger.MANUAL, AssignmentStatus.SUBMITTED))

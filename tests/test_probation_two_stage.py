"""The probation review as ONE form two people fill in.

It used to be three independent forms fired at the same moment. The manager
could receive theirs before the employee opened theirs, submit first, and never
read a word the employee wrote - while the checklist showed "Share probation
form to reporting manager" ticked above "Employee completes self-assessment",
still open. The sequence the page described did not exist.

Pinned here:

* the employee's half settles at the HANDOVER, not at the end, so the
  checklist stops claiming nothing has happened for the days their manager
  takes to write the second half,
* the manager's steps do NOT settle early,
* the manager reads the employee's answers, because they hold the form,
* the employee does NOT read the manager's assessment of them afterwards,
* the line above the manager may READ the review and may not FILL IT IN.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.form import (
    AssignmentStatus,
    Form,
    FormAnswer,
    FormAssignment,
    FormField,
    FormFieldType,
    FormResponse,
    FormStage,
    FormStatus,
    FormTrigger,
)
from app.models.probation_checklist import ProbationChecklistItem, ProbationStepStatus
from tests.conftest import _Seed, auth_headers

_LABELS = ["Poor", "Fair", "Good", "Great", "Outstanding"]
_LEAD_DAYS = 21


# --- fixtures -------------------------------------------------------------- #
async def _two_stage_form(db: AsyncSession) -> Form:
    """One probation review: two questions for the employee, two for their
    manager. A form is two-stage because its FIELDS say so, so this is all it
    takes - there is no flag on the form to set."""
    form = Form(
        family_id=uuid.uuid4(),
        title="Probation Review",
        status=FormStatus.PUBLISHED,
        trigger=FormTrigger.PROBATION_SELF,
        trigger_days=_LEAD_DAYS,
        due_days=7,
        auto_assign_since=date(2020, 1, 1),
        rating_labels=_LABELS,
        published_at=datetime.now(UTC),
    )
    db.add(form)
    await db.flush()
    for position, stage in enumerate(
        (FormStage.EMPLOYEE, FormStage.EMPLOYEE, FormStage.MANAGER, FormStage.MANAGER)
    ):
        db.add(
            FormField(
                form_id=form.id,
                position=position,
                type=FormFieldType.RATING,
                label=f"{stage.value} question {position}",
                stage=stage,
            )
        )
    await db.commit()
    return form


async def _ends_in(db: AsyncSession, person: Employee, days: int) -> None:
    """Put this person's probation end `days` away, so the review is due."""
    person.hire_date = date.today() - timedelta(days=180 - days)
    person.probation_months = 6
    await db.commit()


async def _senior(db: AsyncSession, seed: _Seed) -> Employee:
    """The line above the reporting manager.

    A plain MANAGER two levels up, on purpose: what grants scope is the
    `manager_id` edge HR maintains, not the title beside someone's name
    (`_scope_clause` - "a manager sees their leads' teams"). SENIOR_MANAGER is
    department-scoped instead, which is a different rule and not the one the
    probation line hangs on.
    """
    head = Employee(
        hr_external_id="hr-head",
        work_email="head@corp.test",
        full_name="Hana Head",
        role=Role.MANAGER,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(head)
    await db.flush()
    seed.manager.manager_id = head.id
    await db.commit()
    return head


async def _assignment(db: AsyncSession) -> FormAssignment:
    rows = await db.execute(
        select(FormAssignment).execution_options(populate_existing=True)
    )
    return rows.scalars().one()


async def _steps(db: AsyncSession, person: Employee) -> dict[int, ProbationChecklistItem]:
    rows = await db.execute(
        select(ProbationChecklistItem)
        .where(ProbationChecklistItem.employee_id == person.id)
        .execution_options(populate_existing=True)
    )
    return {item.step_no: item for item in rows.scalars().all()}


def _my_answers(detail: dict, rating: int = 4) -> dict:
    """Answer only the half this caller was asked for.

    `my_field_ids` is the whole point of a two-stage form: answering anything
    else is what the submit refuses.
    """
    mine = set(detail["my_field_ids"])
    return {
        "answers": [
            {"field_id": f["id"], "rating": rating}
            for f in detail["form"]["fields"]
            if f["type"] == "rating" and f["id"] in mine
        ]
    }


async def _open(client: AsyncClient, settings: Settings, who: Employee, aid: uuid.UUID):
    return await client.get(
        f"/api/v1/forms/assignments/{aid}", headers=auth_headers(settings, who)
    )


async def _send(db: AsyncSession, settings: Settings, seed: _Seed) -> FormAssignment:
    """Run the sweep so the review goes out, and return its one assignment."""
    from tests.test_probation_forms import _RecordingEmail, _run, _today

    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _run(db, settings, _RecordingEmail(), await _today(db))
    return await _assignment(db)


# --- the handover ---------------------------------------------------------- #
async def test_the_review_goes_to_the_employee_first(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """A two-stage form starts at its first stage, with the person it is about."""
    assignment = await _send(db, settings, seed)
    assert assignment.assignee_id == seed.report.id
    assert assignment.stage is FormStage.EMPLOYEE
    assert assignment.handed_over_at is None


async def test_employee_half_settles_their_step_and_moves_it_to_the_manager(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    """The bug this whole change exists for.

    `on_submitted` fires only on a FINISHED form. Without the handover hook the
    employee's step would stay open until their manager wrote the second half -
    the checklist showing nothing had happened for days after they had in fact
    done their part.
    """
    assignment = await _send(db, settings, seed)
    detail = (await _open(client, settings, seed.report, assignment.id)).json()

    res = await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail),
        headers=auth_headers(settings, seed.report),
    )
    assert res.status_code == 200, res.text

    moved = await _assignment(db)
    assert moved.status is AssignmentStatus.PENDING  # one form, not two
    assert moved.assignee_id == seed.manager.id
    assert moved.stage is FormStage.MANAGER
    assert moved.handed_over_at is not None

    steps = await _steps(db, seed.report)
    # 3: the employee has done their part. 4: it is with their manager now.
    assert steps[3].status is ProbationStepStatus.DONE
    assert steps[4].status is ProbationStepStatus.DONE
    # and the manager's own steps are NOT claimed before they have written them
    assert 5 not in steps or steps[5].status is not ProbationStepStatus.DONE
    assert 6 not in steps or steps[6].status is not ProbationStepStatus.DONE


async def test_manager_half_settles_the_manager_steps(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    assignment = await _send(db, settings, seed)
    detail = (await _open(client, settings, seed.report, assignment.id)).json()
    await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail),
        headers=auth_headers(settings, seed.report),
    )

    detail = (await _open(client, settings, seed.manager, assignment.id)).json()
    res = await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail),
        headers=auth_headers(settings, seed.manager),
    )
    assert res.status_code == 200, res.text

    done = await _assignment(db)
    assert done.status is AssignmentStatus.SUBMITTED
    steps = await _steps(db, seed.report)
    assert steps[5].status is steps[6].status is ProbationStepStatus.DONE


# --- who reads what -------------------------------------------------------- #
async def test_the_manager_reads_what_the_employee_wrote(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    """The thing that was actually asked for: context before judgement."""
    assignment = await _send(db, settings, seed)
    detail = (await _open(client, settings, seed.report, assignment.id)).json()
    employee_fields = set(detail["my_field_ids"])
    await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail, rating=5),
        headers=auth_headers(settings, seed.report),
    )

    seen = (await _open(client, settings, seed.manager, assignment.id)).json()
    answered = {a["field_id"]: a for a in seen["answers"]}
    assert employee_fields <= set(answered), "the manager cannot see the employee's half"
    assert all(answered[f]["rating"] == 5 for f in employee_fields)
    # ...and is asked only for their OWN questions, so they cannot overwrite it
    assert not (set(seen["my_field_ids"]) & employee_fields)


async def test_the_employee_does_not_read_the_managers_assessment_of_them(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    """They keep their own words and nothing else. An appraisal's second half
    is not theirs to read just because they started the form."""
    assignment = await _send(db, settings, seed)
    detail = (await _open(client, settings, seed.report, assignment.id)).json()
    employee_fields = set(detail["my_field_ids"])
    await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail),
        headers=auth_headers(settings, seed.report),
    )
    detail = (await _open(client, settings, seed.manager, assignment.id)).json()
    await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail, rating=2),
        headers=auth_headers(settings, seed.manager),
    )

    back = (await _open(client, settings, seed.report, assignment.id)).json()
    assert back["own_half_only"] is True
    assert {a["field_id"] for a in back["answers"]} == employee_fields


# --- oversight ------------------------------------------------------------- #
async def test_the_line_above_the_manager_may_read_the_review(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    head = await _senior(db, seed)
    assignment = await _send(db, settings, seed)
    detail = (await _open(client, settings, seed.report, assignment.id)).json()
    await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail),
        headers=auth_headers(settings, seed.report),
    )

    res = await _open(client, settings, head, assignment.id)
    assert res.status_code == 200, res.text
    body = res.json()
    # Oversight reads the whole review, not a half.
    assert body["own_half_only"] is False
    assert body["answers"], "a reader with no answers is not oversight"


async def test_oversight_is_read_only(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    """A department head may read the review. They may not fill it in on a
    manager's behalf - which is why this is not part of `_may_open`."""
    head = await _senior(db, seed)
    assignment = await _send(db, settings, seed)
    detail = (await _open(client, settings, seed.report, assignment.id)).json()
    await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail),
        headers=auth_headers(settings, seed.report),
    )

    mine = (await _open(client, settings, head, assignment.id)).json()
    assert mine["my_field_ids"] == [], "oversight must not be offered the questions"
    res = await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json={"answers": []},
        headers=auth_headers(settings, head),
    )
    assert res.status_code == 404, res.text


async def test_the_probationer_never_reads_it_as_oversight(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    """`can_read` includes yourself. Nobody reads their own probation review,
    so the subject check has to come first."""
    assignment = await _send(db, settings, seed)
    detail = (await _open(client, settings, seed.report, assignment.id)).json()
    await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail),
        headers=auth_headers(settings, seed.report),
    )
    # They are no longer the assignee; they get their own half back and no more.
    back = (await _open(client, settings, seed.report, assignment.id)).json()
    assert back["own_half_only"] is True


async def test_someone_outside_the_tree_gets_nothing(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    assignment = await _send(db, settings, seed)
    res = await _open(client, settings, seed.outsider, assignment.id)
    assert res.status_code == 404, res.text


@pytest.mark.parametrize("rating", [1, 5])
async def test_both_halves_land_in_one_set_of_answers(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient, rating: int
) -> None:
    """One form, one response, two authors - the reason for doing it this way
    rather than two documents nobody lines up afterwards."""
    assignment = await _send(db, settings, seed)
    detail = (await _open(client, settings, seed.report, assignment.id)).json()
    await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail, rating=rating),
        headers=auth_headers(settings, seed.report),
    )
    detail = (await _open(client, settings, seed.manager, assignment.id)).json()
    await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail, rating=rating),
        headers=auth_headers(settings, seed.manager),
    )

    final = (await _open(client, settings, seed.admin, assignment.id)).json()
    assert len(final["answers"]) == 4, "both halves must land in ONE set of answers"

    # Authorship is not an API field - each field's `stage` is what tells a
    # reader whose half it is - but the column underneath has to be right, or
    # `own_half_only` hands the employee their manager's words.
    rows = await db.execute(
        select(FormAnswer.answered_by_id)
        .join(FormResponse, FormResponse.id == FormAnswer.response_id)
        .where(FormResponse.assignment_id == assignment.id)
    )
    authors = set(rows.scalars().all())
    assert authors == {seed.report.id, seed.manager.id}


# --- what the merge must not break ----------------------------------------- #
async def test_a_null_stage_is_not_the_first_stage(db: AsyncSession) -> None:
    """The trap the merge migration has to defuse.

    An assignment created before the form had stages carries stage = NULL, and
    NULL does not mean "start at the beginning" - it means "this form has no
    stages". Left alone, the probationer is handed EVERY question, their
    manager's half included, and submitting ends the review with no handover.
    """
    from app.services.form_stages import fields_for_stage, next_stage

    form = await _two_stage_form(db)
    fields = (
        await db.execute(select(FormField).where(FormField.form_id == form.id))
    ).scalars().all()

    assert len(fields_for_stage(list(fields), None)) == 4, "NULL sees the whole form"
    assert next_stage(list(fields), None) is None, "NULL never hands over"
    # ...whereas the stage the migration writes sees half, and moves on.
    assert len(fields_for_stage(list(fields), FormStage.EMPLOYEE)) == 2
    assert next_stage(list(fields), FormStage.EMPLOYEE) is FormStage.MANAGER


def test_the_manager_submit_does_not_restamp_the_employee_step() -> None:
    """`mark_done_by_system` REFRESHES its own earlier ticks. Listing step 3
    under the final submit as well would stamp it with the manager's
    timestamp, so the checklist would say the employee wrote their
    self-assessment the moment their manager finished reading it."""
    from app.services.probation_forms import steps_on_submit

    merged = FormAssignment(
        form_id=uuid.uuid4(),
        trigger=FormTrigger.PROBATION_SELF,
        stage=FormStage.MANAGER,
        status=AssignmentStatus.PENDING,
    )
    assert set(steps_on_submit(merged)) == {5, 6}

    # the single-stage form, as it was sent before the merge
    legacy = FormAssignment(
        form_id=uuid.uuid4(),
        trigger=FormTrigger.PROBATION_SELF,
        stage=None,
        status=AssignmentStatus.PENDING,
    )
    assert set(steps_on_submit(legacy)) == {3}


def test_hr_feedback_is_never_overseeable() -> None:
    """HR's own judgement on the same person is a different form with a
    different audience. The first cut of the oversight rule said "any
    probation form" and handed it to the reporting line."""
    from app.services.probation_forms import OVERSEEABLE_PROBATION

    assert FormTrigger.PROBATION_HR not in OVERSEEABLE_PROBATION
    assert FormTrigger.PROBATION_SELF in OVERSEEABLE_PROBATION


async def test_a_probationer_with_no_manager_parks_the_review(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    """The review cannot be "with the manager" when there isn't one.

    A probationer with no `manager_id` leaves the form parked as NEEDS_MANAGER
    with no assignee. Ticking step 4 there tells HR the review has moved on
    while it is in fact waiting for them - the checklist claiming something
    happened that did not, which is the whole failure this feature exists to
    stop.
    """
    assignment = await _send(db, settings, seed)
    # HR has not set a reporting manager on this person.
    person = await db.get(Employee, seed.report.id)
    assert person is not None
    person.manager_id = None
    await db.commit()

    detail = (await _open(client, settings, seed.report, assignment.id)).json()
    res = await client.post(
        f"/api/v1/forms/assignments/{assignment.id}/submit",
        json=_my_answers(detail),
        headers=auth_headers(settings, seed.report),
    )
    assert res.status_code == 200, res.text

    parked = await _assignment(db)
    assert parked.status is AssignmentStatus.NEEDS_MANAGER
    assert parked.assignee_id is None

    steps = await _steps(db, seed.report)
    assert steps[3].status is ProbationStepStatus.DONE, "they did do their half"
    assert 4 not in steps or steps[4].status is not ProbationStepStatus.DONE, (
        "step 4 claims the review reached a manager that does not exist"
    )


def test_handover_steps_depend_on_whether_a_manager_was_found() -> None:
    from app.services.probation_forms import steps_on_handover

    reached = FormAssignment(
        form_id=uuid.uuid4(),
        trigger=FormTrigger.PROBATION_SELF,
        stage=FormStage.MANAGER,
        status=AssignmentStatus.PENDING,
        assignee_id=uuid.uuid4(),
    )
    assert set(steps_on_handover(reached)) == {3, 4}

    parked = FormAssignment(
        form_id=uuid.uuid4(),
        trigger=FormTrigger.PROBATION_SELF,
        stage=FormStage.MANAGER,
        status=AssignmentStatus.NEEDS_MANAGER,
        assignee_id=None,
    )
    assert set(steps_on_handover(parked)) == {3}

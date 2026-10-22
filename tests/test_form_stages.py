"""Forms two people fill in: the employee's half, then their manager's.

Built for the appraisal case - the person writes their own assessment and
their reporting manager writes the rest, on the SAME form, so both halves end
up in one set of answers.

The things that would quietly go wrong, pinned here:
- the assignment stays PENDING across the handover (it is one form, not two);
- the manager cannot answer, or overwrite, the employee's half;
- the employee cannot answer the manager's half either;
- both halves land in ONE response, each attributed to whoever wrote it;
- a missing manager parks the form instead of losing it;
- an ordinary single-stage form is completely unaffected.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee
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
from app.services.form_stages import (
    fields_for_stage,
    first_stage,
    is_staged,
    next_stage,
    stage_has_questions,
    stages_used,
)
from tests.conftest import _Seed, auth_headers

_LABELS = ["1", "2", "3", "4", "5"]


# --------------------------------------------------------------------------- #
# The pure rules
# --------------------------------------------------------------------------- #
def _field(stage: FormStage | None, *, kind: FormFieldType = FormFieldType.PARAGRAPH) -> FormField:
    return FormField(id=uuid.uuid4(), position=0, type=kind, label="q", stage=stage)


def test_a_form_is_staged_only_if_a_field_says_so() -> None:
    """No flag on the form to drift out of step with its questions."""
    assert not is_staged([_field(None), _field(None)])
    assert is_staged([_field(None), _field(FormStage.MANAGER)])


def test_stages_are_used_in_filling_order() -> None:
    fields = [_field(FormStage.MANAGER), _field(FormStage.EMPLOYEE)]
    assert stages_used(fields) == [FormStage.EMPLOYEE, FormStage.MANAGER]


def test_a_manager_only_form_starts_at_the_manager() -> None:
    """Legitimate, and it must not wait at an empty employee stage."""
    fields = [_field(FormStage.MANAGER)]
    assert first_stage(fields) is FormStage.MANAGER
    assert next_stage(fields, FormStage.MANAGER) is None


def test_an_ordinary_form_has_no_stages() -> None:
    fields = [_field(None)]
    assert first_stage(fields) is None
    assert next_stage(fields, None) is None
    assert fields_for_stage(fields, None) == fields


def test_each_stage_sees_only_its_own_fields() -> None:
    mine, theirs = _field(FormStage.EMPLOYEE), _field(FormStage.MANAGER)
    assert fields_for_stage([mine, theirs], FormStage.EMPLOYEE) == [mine]
    assert fields_for_stage([mine, theirs], FormStage.MANAGER) == [theirs]


def test_a_stage_of_only_headings_has_nothing_to_answer() -> None:
    heading = _field(FormStage.MANAGER, kind=FormFieldType.SECTION)
    assert not stage_has_questions([heading], FormStage.MANAGER)
    assert stage_has_questions([heading, _field(FormStage.MANAGER)], FormStage.MANAGER)


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
async def _two_stage_form(db: AsyncSession) -> tuple[Form, FormField, FormField]:
    form = Form(
        family_id=uuid.uuid4(),
        title="Annual appraisal",
        status=FormStatus.PUBLISHED,
        trigger=FormTrigger.MANUAL,
        due_days=7,
        rating_labels=_LABELS,
        published_at=datetime.now(UTC),
    )
    db.add(form)
    await db.flush()
    mine = FormField(
        form_id=form.id,
        position=0,
        type=FormFieldType.PARAGRAPH,
        label="What went well for you?",
        stage=FormStage.EMPLOYEE,
    )
    theirs = FormField(
        form_id=form.id,
        position=1,
        type=FormFieldType.PARAGRAPH,
        label="Manager's assessment",
        stage=FormStage.MANAGER,
    )
    db.add_all([mine, theirs])
    await db.commit()
    return form, mine, theirs


async def _send(client: AsyncClient, settings: Settings, admin: Employee, form: Form, to: Employee):
    return await client.post(
        f"/api/v1/forms/{form.id}/assignments",
        json={"audience": {"kind": "people", "employee_ids": [str(to.id)]}},
        headers=auth_headers(settings, admin),
    )


async def _assignment(db: AsyncSession) -> FormAssignment:
    rows = await db.execute(select(FormAssignment).execution_options(populate_existing=True))
    return rows.scalars().one()


async def _submit(
    client: AsyncClient,
    settings: Settings,
    who: Employee,
    assignment_id: uuid.UUID | str,
    field: FormField,
    text: str,
):
    return await client.post(
        f"/api/v1/forms/assignments/{assignment_id}/submit",
        json={"answers": [{"field_id": str(field.id), "text": text}]},
        headers=auth_headers(settings, who),
    )


async def test_the_form_starts_with_the_employee(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    form, _, _ = await _two_stage_form(db)

    res = await _send(client, settings, seed.admin, form, seed.report)

    assert res.status_code == 201, res.text
    created = await _assignment(db)
    assert created.stage is FormStage.EMPLOYEE
    assert created.assignee_id == seed.report.id


async def test_the_employee_sees_only_their_half(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    form, mine, _ = await _two_stage_form(db)
    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)

    res = await client.get(
        f"/api/v1/forms/assignments/{created.id}", headers=auth_headers(settings, seed.report)
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["my_field_ids"] == [str(mine.id)]
    assert body["stage"] == "employee"
    assert body["next_stage"] == "manager"


async def test_submitting_hands_it_to_the_manager_and_stays_pending(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """One form being filled in by two people, not two forms."""
    form, mine, _ = await _two_stage_form(db)
    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)

    res = await _submit(client, settings, seed.report, created.id, mine, "Shipped a lot.")

    assert res.status_code == 200, res.text
    moved = await _assignment(db)
    assert moved.status is AssignmentStatus.PENDING
    assert moved.stage is FormStage.MANAGER
    assert moved.assignee_id == seed.report.manager_id
    assert moved.submitted_at is None  # not finished until the manager is done
    assert moved.handed_over_at is not None
    # Cleared so the scheduler tells the NEW holder it is theirs.
    assert moved.notified_at is None


async def test_the_employee_cannot_answer_the_managers_half(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    form, _, theirs = await _two_stage_form(db)
    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)

    res = await _submit(client, settings, seed.report, created.id, theirs, "Sneaking in")

    assert res.status_code == 422, res.text


async def test_the_manager_cannot_overwrite_the_employees_half(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Those fields are not theirs to submit, so there is nothing to overwrite
    with - the point of splitting the form in the first place."""
    form, mine, _ = await _two_stage_form(db)
    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)
    await _submit(client, settings, seed.report, created.id, mine, "My own words.")

    res = await _submit(client, settings, seed.manager, created.id, mine, "Rewritten")

    assert res.status_code == 422, res.text
    rows = await db.execute(select(FormAnswer).execution_options(populate_existing=True))
    kept = [a for a in rows.scalars().all() if a.field_id == mine.id]
    assert len(kept) == 1
    assert kept[0].text == "My own words."


async def test_the_manager_finishes_it_and_both_halves_are_in_one_response(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    form, mine, theirs = await _two_stage_form(db)
    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)
    await _submit(client, settings, seed.report, created.id, mine, "My own words.")

    res = await _submit(client, settings, seed.manager, created.id, theirs, "Agreed, strong year.")

    assert res.status_code == 200, res.text
    done = await _assignment(db)
    assert done.status is AssignmentStatus.SUBMITTED
    assert done.submitted_at is not None

    responses = await db.execute(select(FormResponse).execution_options(populate_existing=True))
    assert len(list(responses.scalars().all())) == 1  # ONE response, two authors

    rows = await db.execute(select(FormAnswer).execution_options(populate_existing=True))
    answers = {a.field_id: a for a in rows.scalars().all()}
    assert answers[mine.id].answered_by_id == seed.report.id
    assert answers[theirs.id].answered_by_id == seed.manager.id


async def test_a_missing_manager_parks_the_form_rather_than_losing_it(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    form, mine, _ = await _two_stage_form(db)
    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)
    seed.report.manager_id = None
    await db.commit()

    res = await _submit(client, settings, seed.report, created.id, mine, "Done my bit.")

    assert res.status_code == 200, res.text
    parked = await _assignment(db)
    assert parked.status is AssignmentStatus.NEEDS_MANAGER
    assert parked.assignee_id is None
    assert parked.stage is FormStage.MANAGER


async def test_nobody_else_can_fill_the_managers_half(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    form, mine, theirs = await _two_stage_form(db)
    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)
    await _submit(client, settings, seed.report, created.id, mine, "My own words.")

    for actor in (seed.report, seed.outsider):
        res = await _submit(client, settings, actor, created.id, theirs, "Not mine to write")
        assert res.status_code == 404, f"{actor.full_name}: {res.text}"


# --------------------------------------------------------------------------- #
# The ordinary form is untouched
# --------------------------------------------------------------------------- #
async def test_a_single_stage_form_still_submits_in_one_go(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    form = Form(
        family_id=uuid.uuid4(),
        title="Plain form",
        status=FormStatus.PUBLISHED,
        trigger=FormTrigger.MANUAL,
        due_days=7,
        rating_labels=_LABELS,
        published_at=datetime.now(UTC),
    )
    db.add(form)
    await db.flush()
    only = FormField(form_id=form.id, position=0, type=FormFieldType.PARAGRAPH, label="How was it?")
    db.add(only)
    await db.commit()

    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)
    assert created.stage is None

    res = await _submit(client, settings, seed.report, created.id, only, "Fine.")

    assert res.status_code == 200, res.text
    done = await _assignment(db)
    assert done.status is AssignmentStatus.SUBMITTED
    assert done.submitted_at is not None


# --------------------------------------------------------------------------- #
# The nightly sweep must not eat a half-filled form
# --------------------------------------------------------------------------- #
async def _sweep(db: AsyncSession, settings: Settings) -> None:
    """Run the settlement sweep the forms scheduler runs every night.

     first: the submit went through the API on its OWN session, so
    this one still holds the pre-handover assignment. Without it the sweep
    would judge a stale row and the test would pass for the wrong reason.
    """
    from tests.test_probation_forms import _RecordingEmail, _service, _today

    db.expire_all()
    service = _service(db, settings, _RecordingEmail())
    await service.run_due(await _today(db))
    await db.commit()


async def test_the_nightly_sweep_does_not_cancel_a_parked_handover(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The regression this exists for: a two-stage form parked at
    NEEDS_MANAGER has no assignee, which used to fall straight to the
    "nobody left to fill it in" rule and destroy the employee's half."""
    form, mine, _ = await _two_stage_form(db)
    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)
    seed.report.manager_id = None
    await db.commit()
    await _submit(client, settings, seed.report, created.id, mine, "My own words.")

    await _sweep(db, settings)

    survived = await _assignment(db)
    assert survived.status is AssignmentStatus.NEEDS_MANAGER
    rows = await db.execute(select(FormAnswer).execution_options(populate_existing=True))
    assert [a.text for a in rows.scalars().all()] == ["My own words."]


async def test_the_sweep_hands_it_on_once_a_manager_is_set(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    form, mine, _ = await _two_stage_form(db)
    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)
    seed.report.manager_id = None
    await db.commit()
    await _submit(client, settings, seed.report, created.id, mine, "My own words.")

    seed.report.manager_id = seed.manager.id
    await db.commit()
    await _sweep(db, settings)

    moved = await _assignment(db)
    assert moved.status is AssignmentStatus.PENDING
    assert moved.assignee_id == seed.manager.id


async def test_a_handover_is_cancelled_when_the_person_leaves(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Recording the subject at handover also makes the ordinary "they left"
    rule work: there is no appraisal to finish for someone who has gone."""
    form, mine, _ = await _two_stage_form(db)
    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)
    await _submit(client, settings, seed.report, created.id, mine, "My own words.")
    seed.report.is_active = False
    await db.commit()

    await _sweep(db, settings)

    gone = await _assignment(db)
    assert gone.status is AssignmentStatus.CANCELLED


def test_a_stage_with_only_headings_is_skipped() -> None:
    """Otherwise the employee gets a form with nothing on it and no way to
    move it along."""
    heading = _field(FormStage.EMPLOYEE, kind=FormFieldType.SECTION)
    question = _field(FormStage.MANAGER)
    fields = [heading, question]

    assert stages_used(fields) == [FormStage.MANAGER]
    assert first_stage(fields) is FormStage.MANAGER


async def test_the_employee_reads_their_own_half_and_hr_reads_both(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """After handing over, the employee is no longer the assignee. They must
    still see what THEY wrote - a 404 on your own form ten seconds after
    submitting it is indefensible - but an appraisal's second half is the
    manager's assessment of them, not theirs to read."""
    form, mine, theirs = await _two_stage_form(db)
    await _send(client, settings, seed.admin, form, seed.report)
    created = await _assignment(db)
    await _submit(client, settings, seed.report, created.id, mine, "My own words.")
    await _submit(client, settings, seed.manager, created.id, theirs, "Manager's view.")

    theirs_view = await client.get(
        f"/api/v1/forms/assignments/{created.id}", headers=auth_headers(settings, seed.report)
    )
    hr_view = await client.get(
        f"/api/v1/forms/assignments/{created.id}", headers=auth_headers(settings, seed.admin)
    )

    assert theirs_view.status_code == 200, theirs_view.text
    assert theirs_view.json()["own_half_only"] is True
    assert [a["text"] for a in theirs_view.json()["answers"]] == ["My own words."]

    assert hr_view.status_code == 200, hr_view.text
    assert hr_view.json()["own_half_only"] is False
    assert sorted(a["text"] for a in hr_view.json()["answers"]) == [
        "Manager's view.",
        "My own words.",
    ]


def test_a_section_travels_with_the_half_it_introduces() -> None:
    """Headings are staged like questions, so each person sees their own half
    laid out as written rather than a bare list of questions."""
    heading = _field(FormStage.EMPLOYEE, kind=FormFieldType.SECTION)
    question = _field(FormStage.EMPLOYEE)
    theirs = _field(FormStage.MANAGER)

    mine = fields_for_stage([heading, question, theirs], FormStage.EMPLOYEE)

    assert mine == [heading, question]

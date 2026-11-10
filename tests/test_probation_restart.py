"""Restarting a probation review, and the stage a hand-sent review starts at.

Two things meet here, and they were one bug.

The review became ONE two-stage form: the employee writes their half, the
assignment moves to their reporting manager, who writes the rest. A form is
two-stage because its FIELDS say so, and whose turn it is lives on the
assignment's `stage`. A null stage does NOT mean "the first stage" - it means
"this form has no stages", and `fields_for_stage` then hands the assignee
EVERY field while `next_stage` returns None. So a probation assignment created
without a stage gives the probationer their manager's assessment of them to
fill in, and finishes the whole review on their submit with no handover.

`_create_probation_due` and `assign` both set it. `send_probation_form` - the
"Send form" button on the checklist - did not, so every review sent by hand
was in that state.

The second half: a review already in that state could not be repaired through
the API at all. `uq_form_assignments_trigger_subject` is unique on (trigger,
subject) for every non-manual form and does NOT exclude cancelled rows, so
withdrawing the broken one and sending a fresh one is not something the
database allows. Restart therefore REUSES the row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee
from app.models.form import (
    AssignmentStatus,
    FormAssignment,
    FormResponse,
    FormStage,
    FormTrigger,
)
from app.models.probation_checklist import ProbationChecklistItem, ProbationStepStatus
from app.models.probation_decision import ProbationDecision, ProbationOutcome
from tests.conftest import _Seed, auth_headers
from tests.test_probation_forms import _ends_in, _hr, _live_form
from tests.test_probation_two_stage import _two_stage_form


def _url(person: Employee) -> str:
    return f"/api/v1/probation/{person.id}/review/restart"


async def _restart(client: AsyncClient, settings: Settings, who: Employee, subject: Employee):
    return await client.post(_url(subject), headers=auth_headers(settings, who))


async def _send_by_hand(
    client: AsyncClient,
    settings: Settings,
    who: Employee,
    subject: Employee,
    trigger: FormTrigger = FormTrigger.PROBATION_SELF,
):
    return await client.post(
        f"/api/v1/probation/{subject.id}/forms/{trigger.value}/send",
        headers=auth_headers(settings, who),
    )


async def _rows(db: AsyncSession, subject: Employee) -> list[FormAssignment]:
    res = await db.execute(
        select(FormAssignment)
        .where(FormAssignment.subject_id == subject.id)
        .order_by(FormAssignment.created_at)
        .execution_options(populate_existing=True)
    )
    return list(res.scalars().all())


async def _steps(db: AsyncSession, subject: Employee) -> dict[int, ProbationChecklistItem]:
    res = await db.execute(
        select(ProbationChecklistItem)
        .where(ProbationChecklistItem.employee_id == subject.id)
        .execution_options(populate_existing=True)
    )
    return {row.step_no: row for row in res.scalars().all()}


# --------------------------------------------------------------------------- #
# The bug: a review sent by hand had no stage
# --------------------------------------------------------------------------- #
async def test_a_hand_sent_review_starts_at_the_employee_stage(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The regression. `send_probation_form` called `add(**values)` with no
    `stage`, so the column took its default of None."""
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)

    res = await _send_by_hand(client, settings, seed.admin, seed.report)
    assert res.status_code == 201, res.text

    (row,) = await _rows(db, seed.report)
    assert row.stage is FormStage.EMPLOYEE, "a two-stage review must start at a stage"


async def test_a_hand_sent_review_asks_the_employee_only_their_own_half(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """What the missing stage actually did: the probationer was handed their
    manager's assessment of them as well as their own questions."""
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    (row,) = await _rows(db, seed.report)

    detail = await client.get(
        f"/api/v1/forms/assignments/{row.id}", headers=auth_headers(settings, seed.report)
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert len(body["my_field_ids"]) == 2, "the employee owns two of the four questions"
    assert len(body["form"]["fields"]) == 4


# --------------------------------------------------------------------------- #
# Who may restart one
# --------------------------------------------------------------------------- #
async def test_unauthenticated_is_rejected(
    client: AsyncClient, seed: _Seed, db: AsyncSession
) -> None:
    assert (await client.post(_url(seed.report))).status_code == 401


async def test_only_hr_and_admins_can_restart(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """404 rather than 403: who is on probation is not everyone's business.

    The probationer themselves included - they must not be able to wipe the
    record of their own review.
    """
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)

    for actor in (seed.report, seed.manager, seed.outsider):
        res = await _restart(client, settings, actor, seed.report)
        assert res.status_code == 404, f"{actor.full_name}: {res.text}"


async def test_hr_can_restart_one(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)

    res = await _restart(client, settings, await _hr(db, "harriet"), seed.report)
    assert res.status_code == 200, res.text


async def test_an_unknown_person_is_a_404(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    res = await client.post(
        f"/api/v1/probation/{uuid.uuid4()}/review/restart",
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 404


async def test_a_leaver_cannot_be_restarted(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    seed.report.is_active = False
    await db.commit()

    assert (await _restart(client, settings, seed.admin, seed.report)).status_code == 404


# --------------------------------------------------------------------------- #
# What it repairs
# --------------------------------------------------------------------------- #
async def test_a_restart_puts_a_stageless_review_back_on_its_feet(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The live case this was written for: a review issued before the two
    forms merged, carrying a null stage on a form that now has stages."""
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    (row,) = await _rows(db, seed.report)
    row.stage = None  # what the old single-stage form left behind
    row.notified_at = datetime.now(UTC)
    await db.commit()

    res = await _restart(client, settings, seed.admin, seed.report)
    assert res.status_code == 200, res.text

    (row,) = await _rows(db, seed.report)
    assert row.stage is FormStage.EMPLOYEE
    assert row.status is AssignmentStatus.PENDING
    assert row.assignee_id == seed.report.id
    assert row.notified_at is None, "cleared, or the scheduler never tells them again"


async def test_a_restart_reuses_the_row_rather_than_sending_a_second_copy(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """`uq_form_assignments_trigger_subject` does not exclude cancelled rows,
    so a withdraw-and-resend would be an IntegrityError, not a restart."""
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    (before,) = await _rows(db, seed.report)
    original = before.id

    res = await _restart(client, settings, seed.admin, seed.report)
    assert res.status_code == 200, res.text

    rows = await _rows(db, seed.report)
    assert len(rows) == 1, "one review, not two"
    assert rows[0].id == original, "the same row, so links already sent still resolve"
    assert res.json()["reset"] == 1
    assert res.json()["issued"] == 0


async def test_a_restart_revives_a_withdrawn_review(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A cancelled row is exactly what a restart is for, and the only thing
    that can bring it back - sending a fresh copy hits the unique index."""
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    (row,) = await _rows(db, seed.report)
    row.status = AssignmentStatus.CANCELLED
    await db.commit()

    assert (await _restart(client, settings, seed.admin, seed.report)).status_code == 200

    (row,) = await _rows(db, seed.report)
    assert row.status is AssignmentStatus.PENDING
    assert row.stage is FormStage.EMPLOYEE


async def test_a_restart_clears_a_half_finished_handover(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A review parked with the manager goes back to the employee, and the
    handover stamp goes with it - otherwise the panel shows it as waiting on
    somebody while the questions are open again."""
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    (row,) = await _rows(db, seed.report)
    row.stage = FormStage.MANAGER
    row.assignee_id = seed.manager.id
    row.handed_over_at = datetime.now(UTC)
    await db.commit()

    assert (await _restart(client, settings, seed.admin, seed.report)).status_code == 200

    (row,) = await _rows(db, seed.report)
    assert row.stage is FormStage.EMPLOYEE
    assert row.assignee_id == seed.report.id
    assert row.handed_over_at is None


# --------------------------------------------------------------------------- #
# The checklist
# --------------------------------------------------------------------------- #
async def test_a_restart_re_ticks_only_the_steps_whose_form_went_out(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Step 4 is "Share probation form to reporting manager". After the merge
    there IS no separate manager form, so a restart must leave that step
    pending rather than tick it for something that never happened - the exact
    lie the two-stage merge existed to remove.
    """
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    # A stale tick from the old three-form process, and a human one.
    db.add(
        ProbationChecklistItem(
            employee_id=seed.report.id,
            step_no=4,
            status=ProbationStepStatus.DONE,
            note="Done in Avora: probation evaluation form sent to the reporting manager.",
        )
    )
    db.add(
        ProbationChecklistItem(
            employee_id=seed.report.id,
            step_no=8,
            status=ProbationStepStatus.DONE,
            actor_id=seed.admin.id,
            acted_at=datetime.now(UTC),
            note="Attendance checked",
        )
    )
    await db.commit()

    res = await _restart(client, settings, seed.admin, seed.report)
    assert res.status_code == 200, res.text

    steps = await _steps(db, seed.report)
    assert 4 not in steps, "no manager form is published, so nothing shared with them"
    assert 8 not in steps, "a restart means the whole review runs again, HR's steps too"
    assert steps[1].status is ProbationStepStatus.DONE, "the review was started"
    assert steps[2].status is ProbationStepStatus.DONE, "the self-assessment went out"
    assert res.json()["steps_cleared"] == 4, "2 from the send, plus the 2 added here"


async def test_a_restart_issues_a_form_the_person_never_had(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A form published after their window opened is the ordinary reason a
    review is incomplete, and a restart picks it up."""
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    await _live_form(db, FormTrigger.PROBATION_HR)  # published late

    res = await _restart(client, settings, seed.admin, seed.report)
    assert res.status_code == 200, res.text
    assert res.json()["reset"] == 1
    assert res.json()["issued"] == 1

    triggers = {row.trigger for row in await _rows(db, seed.report)}
    assert triggers == {FormTrigger.PROBATION_SELF, FormTrigger.PROBATION_HR}


# --------------------------------------------------------------------------- #
# What it refuses, and that a refusal writes nothing
# --------------------------------------------------------------------------- #
async def test_a_restart_will_not_discard_written_answers(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    (row,) = await _rows(db, seed.report)
    db.add(
        FormResponse(
            assignment_id=row.id,
            form_id=row.form_id,
            respondent_id=seed.report.id,
            submitted_at=datetime.now(UTC),
        )
    )
    await db.commit()

    res = await _restart(client, settings, seed.admin, seed.report)
    assert res.status_code == 409, res.text
    assert "answered" in res.json()["error"]["message"].lower()


async def test_a_restart_will_not_un_decide_a_review(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Recording an outcome can move someone's probation end date and may
    already have put a letter in their hands."""
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    db.add(
        ProbationDecision(
            employee_id=seed.report.id,
            outcome=ProbationOutcome.CONFIRMED,
            effective_date=date.today(),
            decided_by=seed.admin.id,
        )
    )
    await db.commit()

    res = await _restart(client, settings, seed.admin, seed.report)
    assert res.status_code == 409, res.text
    assert "decision" in res.json()["error"]["message"].lower()


async def test_a_restart_refuses_somebody_with_no_probation_end(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    seed.report.hire_date = None
    await db.commit()

    res = await _restart(client, settings, seed.admin, seed.report)
    assert res.status_code == 409, res.text
    assert "probation end date" in res.json()["error"]["message"].lower()


@pytest.mark.parametrize("reason", ["answers", "decision", "no_end_date"])
async def test_a_refused_restart_changes_nothing(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession, reason: str
) -> None:
    """Every guard runs BEFORE the first write, so a refusal never leaves a
    review half-restarted - checklist cleared, forms untouched."""
    await _two_stage_form(db)
    await _ends_in(db, seed.report, 10)
    await _send_by_hand(client, settings, seed.admin, seed.report)
    (row,) = await _rows(db, seed.report)
    row.stage = None  # the broken state a restart would fix
    await db.commit()

    if reason == "answers":
        db.add(
            FormResponse(
                assignment_id=row.id,
                form_id=row.form_id,
                respondent_id=seed.report.id,
                submitted_at=datetime.now(UTC),
            )
        )
    elif reason == "decision":
        db.add(
            ProbationDecision(
                employee_id=seed.report.id,
                outcome=ProbationOutcome.CONFIRMED,
                effective_date=date.today(),
                decided_by=seed.admin.id,
            )
        )
    else:
        seed.report.hire_date = None
    await db.commit()
    before = await _steps(db, seed.report)

    assert (await _restart(client, settings, seed.admin, seed.report)).status_code == 409

    assert set(await _steps(db, seed.report)) == set(before), "checklist untouched"
    (after,) = await _rows(db, seed.report)
    assert after.stage is None, "the broken review is left exactly as it was"

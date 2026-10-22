"""Sending a probation review form from the checklist, on demand.

The scheduler already sends these when a review window opens. This is the same
act performed by hand, for the cases the clock does not cover: a probation that
was extended, a form published after someone's window had already opened, a
manager only put on file today.

What matters is that it is the SAME act. A hand-rolled "manual" send would
carry `FormTrigger.MANUAL`, which would leave the checklist step untouched and
the answers outside the probation panel - a form that looks sent but moves
nothing. These tests pin that it behaves exactly like the automatic path.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee
from app.models.form import AssignmentStatus, FormTrigger
from app.models.probation_checklist import ProbationStepStatus
from tests.conftest import _Seed, auth_headers
from tests.test_probation_forms import _assignments, _ends_in, _hr, _live_form, _steps


def _url(employee: Employee, trigger: FormTrigger) -> str:
    return f"/api/v1/probation/{employee.id}/forms/{trigger.value}/send"


async def _send(
    client: AsyncClient,
    settings: Settings,
    who: Employee,
    subject: Employee,
    trigger: FormTrigger = FormTrigger.PROBATION_SELF,
):
    return await client.post(_url(subject, trigger), headers=auth_headers(settings, who))


# --------------------------------------------------------------------------- #
# Who may press it
# --------------------------------------------------------------------------- #
async def test_unauthenticated_is_rejected(
    client: AsyncClient, seed: _Seed, db: AsyncSession
) -> None:
    assert (await client.post(_url(seed.report, FormTrigger.PROBATION_SELF))).status_code == 401


async def test_only_hr_and_admins_can_send(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """404 rather than 403: who is on probation is not everyone's business."""
    await _live_form(db, FormTrigger.PROBATION_SELF)
    await _ends_in(db, seed.report, 10)

    for actor in (seed.report, seed.manager, seed.outsider):
        res = await _send(client, settings, actor, seed.report)
        assert res.status_code == 404, f"{actor.full_name}: {res.text}"


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
async def test_sending_creates_the_same_assignment_the_scheduler_would(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _live_form(db, FormTrigger.PROBATION_SELF)
    await _ends_in(db, seed.report, 10)

    res = await _send(client, settings, seed.admin, seed.report)

    assert res.status_code == 201, res.text
    rows = await _assignments(db)
    assert len(rows) == 1
    created = rows[0]
    # The probation trigger, not MANUAL - that is what ties it to the checklist
    # and the probation panel.
    assert created.trigger is FormTrigger.PROBATION_SELF
    assert created.subject_id == seed.report.id
    assert created.assignee_id == seed.report.id
    assert created.status is AssignmentStatus.PENDING


async def test_sending_ticks_the_step_the_form_owns(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Step 2 is "share employee self-assessment form". Sending it IS doing
    step 2, so the tick is part of the send rather than a second click."""
    await _live_form(db, FormTrigger.PROBATION_SELF)
    await _ends_in(db, seed.report, 10)

    await _send(client, settings, seed.admin, seed.report)

    steps = await _steps(db, seed.report)
    assert steps[2].status is ProbationStepStatus.DONE
    # Ticked as Avora, not as the person who pressed it: the form going out
    # settled the step, not a human judgement.
    assert steps[2].actor_id is None


async def test_the_manager_form_goes_to_the_manager_about_the_probationer(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _live_form(db, FormTrigger.PROBATION_MANAGER)
    await _ends_in(db, seed.report, 10)

    res = await _send(client, settings, seed.admin, seed.report, FormTrigger.PROBATION_MANAGER)

    assert res.status_code == 201, res.text
    created = (await _assignments(db))[0]
    assert created.assignee_id == seed.report.manager_id
    assert created.subject_id == seed.report.id


async def test_hr_can_send_it_too(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db, "hana")
    await _live_form(db, FormTrigger.PROBATION_SELF)
    await _ends_in(db, seed.report, 10)

    assert (await _send(client, settings, hr, seed.report)).status_code == 201


async def test_it_works_outside_the_automatic_window(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The whole point. The scheduler only sends inside the 21-day window; a
    form published late, or a probation that moved, needs a human to send it."""
    await _live_form(db, FormTrigger.PROBATION_SELF)
    await _ends_in(db, seed.report, 120)  # far outside the window

    res = await _send(client, settings, seed.admin, seed.report)

    assert res.status_code == 201, res.text
    assert len(await _assignments(db)) == 1


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
async def test_an_unpublished_form_is_refused_with_a_reason(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _ends_in(db, seed.report, 10)

    res = await _send(client, settings, seed.admin, seed.report)

    assert res.status_code == 409, res.text
    assert "publish" in res.json()["error"]["message"].lower()


async def test_the_same_form_cannot_be_sent_twice(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """One assignment per (trigger, subject), ever - the same rule the database
    enforces. A second copy of a form they may already have answered is noise."""
    await _live_form(db, FormTrigger.PROBATION_SELF)
    await _ends_in(db, seed.report, 10)

    assert (await _send(client, settings, seed.admin, seed.report)).status_code == 201
    again = await _send(client, settings, seed.admin, seed.report)

    assert again.status_code == 409, again.text
    assert len(await _assignments(db)) == 1


async def test_somebody_with_no_probation_end_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _live_form(db, FormTrigger.PROBATION_SELF)
    seed.report.hire_date = None
    await db.commit()

    res = await _send(client, settings, seed.admin, seed.report)

    assert res.status_code == 409, res.text


async def test_an_unknown_person_is_a_404(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _live_form(db, FormTrigger.PROBATION_SELF)
    res = await client.post(
        f"/api/v1/probation/{uuid.uuid4()}/forms/probation_self/send",
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 404


async def test_a_non_probation_form_cannot_be_sent_this_way(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """`new_joiner` is a real trigger but not a probation review form. It has
    its own flow; letting it through here would tick probation steps for it."""
    await _ends_in(db, seed.report, 10)
    res = await client.post(
        f"/api/v1/probation/{seed.report.id}/forms/new_joiner/send",
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 422, res.text


async def test_a_leaver_cannot_be_sent_one(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _live_form(db, FormTrigger.PROBATION_SELF)
    await _ends_in(db, seed.report, 10)
    seed.report.is_active = False
    await db.commit()

    assert (await _send(client, settings, seed.admin, seed.report)).status_code == 404


def test_the_step_mapping_is_the_one_the_server_owns() -> None:
    """The UI mirrors `_ON_OPEN` for its buttons. If this mapping moves, the
    checklist's "Send form" buttons move with it."""
    from app.services.probation_forms import _ON_OPEN

    assert set(_ON_OPEN[FormTrigger.PROBATION_SELF]) == {2}
    assert set(_ON_OPEN[FormTrigger.PROBATION_MANAGER]) == {4}

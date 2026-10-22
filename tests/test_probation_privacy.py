"""Nobody reads their own probation review.

The 16-step checklist is HR's working document: who ticked what, HR's private
notes, how far the manager has got. It is not the probationer's to read, and
until now an employee on probation could open Workforce -> Probation and find
themselves on the roster with the whole thing laid out.

What they keep is `GET /probation/me` - where they stand, the outcome once
there is one, and the one step that is genuinely theirs: acknowledging their
letter. Both halves are pinned here, because a fix that silently took away
their status card would be its own bug.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.probation import ACKNOWLEDGE_STEP
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.probation_checklist import ProbationChecklistItem, ProbationStepStatus
from app.models.probation_decision import ProbationDecision, ProbationOutcome
from tests.conftest import _Seed, auth_headers


async def _on_probation(db: AsyncSession, person: Employee) -> None:
    person.hire_date = date.today() - timedelta(days=30)
    person.probation_months = 6
    await db.commit()


async def _hr(db: AsyncSession) -> Employee:
    person = Employee(
        hr_external_id="hr-privacy",
        work_email="hr.privacy@corp.test",
        full_name="Hana HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
        hire_date=date(2020, 1, 1),
    )
    db.add(person)
    await db.commit()
    return person


async def _confirm(db: AsyncSession, person: Employee) -> None:
    db.add(
        ProbationDecision(
            employee_id=person.id,
            outcome=ProbationOutcome.CONFIRMED,
            effective_date=date.today(),
            decided_at=datetime.now(UTC),
        )
    )
    await db.commit()


# --------------------------------------------------------------------------- #
# The checklist is closed to its subject
# --------------------------------------------------------------------------- #
async def test_an_employee_cannot_read_their_own_checklist(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _on_probation(db, seed.report)

    res = await client.get(
        f"/api/v1/probation/{seed.report.id}", headers=auth_headers(settings, seed.report)
    )

    assert res.status_code == 404, res.text


async def test_not_even_hr_reads_their_own_checklist(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Otherwise HR reads the assessment of themselves, which is the same
    conflict the probation FORMS already refuse."""
    hr = await _hr(db)
    await _on_probation(db, hr)

    res = await client.get(f"/api/v1/probation/{hr.id}", headers=auth_headers(settings, hr))

    assert res.status_code == 404, res.text


async def test_an_employee_never_appears_on_their_own_roster(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The roster is scoped, and "anyone else just themselves" meant a
    probationer opening the page found their own review waiting."""
    await _on_probation(db, seed.report)

    res = await client.get("/api/v1/probation/roster", headers=auth_headers(settings, seed.report))

    assert res.status_code == 200, res.text
    assert str(seed.report.id) not in {r["employee_id"] for r in res.json()}


async def test_hr_still_sees_everyone_elses_review(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The point is the SUBJECT, not HR's access."""
    hr = await _hr(db)
    await _on_probation(db, seed.report)

    roster = await client.get("/api/v1/probation/roster", headers=auth_headers(settings, hr))
    checklist = await client.get(
        f"/api/v1/probation/{seed.report.id}", headers=auth_headers(settings, hr)
    )

    assert str(seed.report.id) in {r["employee_id"] for r in roster.json()}
    assert checklist.status_code == 200, checklist.text
    assert len(checklist.json()["steps"]) > 0


async def test_the_manager_still_sees_their_reports_review(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Steps 5, 6 and 10 are the manager's to move; cutting them out would
    leave those steps unmovable by their owner."""
    await _on_probation(db, seed.report)

    res = await client.get(
        f"/api/v1/probation/{seed.report.id}", headers=auth_headers(settings, seed.manager)
    )

    assert res.status_code == 200, res.text


# --------------------------------------------------------------------------- #
# What they DO keep
# --------------------------------------------------------------------------- #
async def test_their_own_status_is_still_theirs_to_see(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _on_probation(db, seed.report)

    res = await client.get("/api/v1/probation/me", headers=auth_headers(settings, seed.report))

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_on_probation"] is True
    assert body["probation_end_date"] is not None
    assert body["days_to_probation_end"] is not None
    # The machinery is absent, not merely empty.
    assert "steps" not in body
    assert "settled_steps" not in body


async def test_the_outcome_reaches_them_once_hr_records_it(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _on_probation(db, seed.report)
    await _confirm(db, seed.report)

    res = await client.get("/api/v1/probation/me", headers=auth_headers(settings, seed.report))

    assert res.status_code == 200, res.text
    assert res.json()["decision"]["outcome"] == "confirmed"
    assert res.json()["needs_acknowledgement"] is True


async def test_acknowledging_clears_it(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Step 14 is the one thing they may act on, and it must still work."""
    await _on_probation(db, seed.report)
    await _confirm(db, seed.report)
    db.add(
        ProbationChecklistItem(
            employee_id=seed.report.id,
            step_no=ACKNOWLEDGE_STEP,
            status=ProbationStepStatus.DONE,
            acted_at=datetime.now(UTC),
        )
    )
    await db.commit()

    res = await client.get("/api/v1/probation/me", headers=auth_headers(settings, seed.report))

    assert res.json()["needs_acknowledgement"] is False


async def test_somebody_with_nothing_to_report_gets_nothing(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Confirmed years ago with no decision on file: no card, rather than an
    empty one on every dashboard in the company."""
    seed.report.hire_date = date(2020, 1, 1)
    await db.commit()

    res = await client.get("/api/v1/probation/me", headers=auth_headers(settings, seed.report))

    assert res.status_code == 200
    assert res.json() is None


async def test_me_is_not_read_as_an_employee_id(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """`/probation/me` is declared before `/{employee_id}`; the wrong order
    would make it a 422 on an unparseable UUID."""
    res = await client.get("/api/v1/probation/me", headers=auth_headers(settings, seed.admin))
    assert res.status_code == 200


async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/probation/me")).status_code == 401


# --------------------------------------------------------------------------- #
# The one action they own
# --------------------------------------------------------------------------- #
async def test_the_employee_can_still_acknowledge_their_letter(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The regression this exists for: closing the self-read also closed
    `set_step`, which is how they used to sign off - so the fix took away the
    one thing that was genuinely theirs."""
    await _on_probation(db, seed.report)
    await _confirm(db, seed.report)

    res = await client.post(
        "/api/v1/probation/me/acknowledge", headers=auth_headers(settings, seed.report)
    )

    assert res.status_code == 200, res.text
    assert res.json()["needs_acknowledgement"] is False


async def test_acknowledging_is_recorded_against_them(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    await _on_probation(db, seed.report)
    await _confirm(db, seed.report)
    await client.post(
        "/api/v1/probation/me/acknowledge", headers=auth_headers(settings, seed.report)
    )

    # HR sees it on the checklist, signed by them.
    res = await client.get(
        f"/api/v1/probation/{seed.report.id}", headers=auth_headers(settings, hr)
    )

    step = next(s for s in res.json()["steps"] if s["no"] == ACKNOWLEDGE_STEP)
    assert step["status"] == "done"
    assert step["actor_id"] == str(seed.report.id)


async def test_there_is_nothing_to_acknowledge_before_a_decision(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _on_probation(db, seed.report)

    res = await client.post(
        "/api/v1/probation/me/acknowledge", headers=auth_headers(settings, seed.report)
    )

    assert res.status_code == 409, res.text


async def test_nobody_is_asked_to_sign_off_their_own_dismissal(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _on_probation(db, seed.report)
    db.add(
        ProbationDecision(
            employee_id=seed.report.id,
            outcome=ProbationOutcome.TERMINATED,
            effective_date=date.today(),
            decided_at=datetime.now(UTC),
        )
    )
    await db.commit()

    status = await client.get("/api/v1/probation/me", headers=auth_headers(settings, seed.report))
    ack = await client.post(
        "/api/v1/probation/me/acknowledge", headers=auth_headers(settings, seed.report)
    )

    assert status.json()["needs_acknowledgement"] is False
    assert ack.status_code == 409, ack.text


async def test_the_employee_still_cannot_move_any_other_step(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The general step endpoint reads the whole checklist to answer, so it is
    closed to them entirely - acknowledge is the only door."""
    await _on_probation(db, seed.report)

    res = await client.put(
        f"/api/v1/probation/{seed.report.id}/steps/1",
        json={"status": "done", "note": None},
        headers=auth_headers(settings, seed.report),
    )

    assert res.status_code == 404, res.text


async def test_the_subject_cannot_read_their_own_review_form_panel(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The answers and the score were already protected, but the panel still
    told them each form's status, who was reviewing them and when it came
    back - the same machinery the checklist stopped showing."""
    await _on_probation(db, seed.report)

    res = await client.get(
        f"/api/v1/forms/probation/{seed.report.id}", headers=auth_headers(settings, seed.report)
    )

    assert res.status_code == 404, res.text


async def test_hr_still_reads_the_review_form_panel(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    await _on_probation(db, seed.report)

    res = await client.get(
        f"/api/v1/forms/probation/{seed.report.id}", headers=auth_headers(settings, hr)
    )

    assert res.status_code == 200, res.text
    assert len(res.json()["forms"]) == 3

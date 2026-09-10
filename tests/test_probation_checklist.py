"""Probation review checklist — the 16-step process, tracked per employee.

Two things matter beyond "does it store a tick". First, scope: who is under
review is itself sensitive, so an out-of-scope caller must get 404, not data
(CLAUDE.md §9). Second, step OWNERSHIP: the checklist records who was accountable
at each point, so HR ticking the employee's own self-assessment would make the
record a formality. Both are enforced server-side and both are pinned here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.probation import REVIEW_LEAD_DAYS, STEPS, StepOwner
from app.models.employee import Employee, Role
from tests.conftest import _Seed, auth_headers

_PROBATION_MONTHS = 6
# Steps whose owner makes them useful test subjects.
_HR_STEP = 1  # "Initiate probation review process"
_EMPLOYEE_STEP = 3  # "Employee completes self-assessment"
_MANAGER_STEP = 5  # "Reporting manager reviews performance"


async def _set_hire_date(db: AsyncSession, employee_id: object, hire: date) -> None:
    person = await db.get(Employee, employee_id)
    assert person is not None
    person.hire_date = hire
    await db.commit()


async def _put_probationer(db: AsyncSession, seed: _Seed) -> None:
    """Joined a month ago, so comfortably inside a 6-month probation."""
    await _set_hire_date(db, seed.report.id, datetime.now(UTC).date() - timedelta(days=30))


async def _checklist(client: AsyncClient, settings: Settings, caller: Employee, target: Employee):
    return await client.get(
        f"/api/v1/probation/{target.id}", headers=auth_headers(settings, caller)
    )


async def _set_step(
    client: AsyncClient,
    settings: Settings,
    caller: Employee,
    target: Employee,
    step_no: int,
    status: str = "done",
    note: str | None = None,
):
    return await client.put(
        f"/api/v1/probation/{target.id}/steps/{step_no}",
        headers=auth_headers(settings, caller),
        json={"status": status, "note": note},
    )


# --- the template ----------------------------------------------------------- #
def test_step_eleven_is_absent_and_numbers_are_stable() -> None:
    """Step 11 was struck out and folded into 12. The remaining numbers keep the
    values HR uses in their own sheet, so 11 is a gap, not a renumbering."""
    numbers = [step.no for step in STEPS]
    assert 11 not in numbers
    assert numbers == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16]


def test_every_step_has_an_owner_and_a_title() -> None:
    for step in STEPS:
        assert step.title
        assert isinstance(step.owner, StepOwner)


# --- reads ------------------------------------------------------------------ #
async def test_checklist_starts_all_pending_with_derived_dates(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _put_probationer(db, seed)

    res = await _checklist(client, settings, seed.admin, seed.report)

    assert res.status_code == 200
    body = res.json()
    assert body["is_on_probation"] is True
    assert body["settled_steps"] == 0
    assert body["total_steps"] == len(STEPS)
    assert len(body["steps"]) == len(STEPS)
    assert [s["position"] for s in body["steps"]] == list(range(1, len(STEPS) + 1))
    assert all(s["status"] == "pending" for s in body["steps"])
    # The review is meant to start three weeks out, so the UI can say when.
    assert body["review_start_date"] is not None
    assert (
        date.fromisoformat(body["probation_end_date"])
        - date.fromisoformat(body["review_start_date"])
    ).days == REVIEW_LEAD_DAYS


async def test_roster_lists_only_people_actually_on_probation(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _put_probationer(db, seed)
    # The outsider joined two years ago, so they are tenured, not under review.
    await _set_hire_date(db, seed.outsider.id, datetime.now(UTC).date() - timedelta(days=730))

    res = await client.get("/api/v1/probation/roster", headers=auth_headers(settings, seed.admin))

    assert res.status_code == 200
    names = [row["employee_name"] for row in res.json()]
    assert seed.report.full_name in names
    assert seed.outsider.full_name not in names


async def test_a_finished_review_stays_reachable_after_confirmation(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Dropping people the day they confirm made a completed 16-step review
    unreachable: the rows were still there, but nothing in the UI linked to
    them. They stay listed for a while, flagged as no longer on probation."""
    # Confirmed a fortnight ago: 6 months probation, joined 6.5 months back.
    await _set_hire_date(db, seed.report.id, datetime.now(UTC).date() - timedelta(days=195))

    res = await client.get("/api/v1/probation/roster", headers=auth_headers(settings, seed.admin))

    assert res.status_code == 200
    row = next(r for r in res.json() if r["employee_name"] == seed.report.full_name)
    assert row["on_probation"] is False
    assert row["review_due"] is False  # nothing left to chase


async def test_a_long_confirmed_employee_drops_off_the_roster(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _set_hire_date(db, seed.report.id, datetime.now(UTC).date() - timedelta(days=400))

    res = await client.get("/api/v1/probation/roster", headers=auth_headers(settings, seed.admin))

    assert res.status_code == 200
    assert res.json() == []


async def test_someone_with_no_hire_date_is_not_on_the_roster(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """No joining date means no probation window to compute, so they cannot be
    listed as due for review - three real employees are in exactly that state."""
    res = await client.get("/api/v1/probation/roster", headers=auth_headers(settings, seed.admin))

    assert res.status_code == 200
    assert res.json() == []


# --- authorization ---------------------------------------------------------- #
async def test_an_outsider_cannot_read_someone_elses_checklist(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """404 rather than 403: that a named person is under probation review is
    itself scope leakage."""
    await _put_probationer(db, seed)

    res = await _checklist(client, settings, seed.outsider, seed.report)

    assert res.status_code == 404


async def test_the_employee_and_their_manager_can_both_read_it(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _put_probationer(db, seed)

    assert (await _checklist(client, settings, seed.report, seed.report)).status_code == 200
    assert (await _checklist(client, settings, seed.manager, seed.report)).status_code == 200


async def test_a_senior_manager_sees_their_whole_department(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Scope must come from the shared repository clause. A hand-rolled "are you
    their manager?" check here excluded senior managers, who read their whole
    department everywhere else in the app - the report is in their department but
    reports to someone else."""
    await _put_probationer(db, seed)
    boss = await db.get(Employee, seed.outsider.id)
    person = await db.get(Employee, seed.report.id)
    assert boss is not None and person is not None
    boss.role = Role.SENIOR_MANAGER
    boss.department = "Engineering"
    person.department = "Engineering"
    await db.commit()

    res = await _checklist(client, settings, seed.outsider, seed.report)
    assert res.status_code == 200

    roster = await client.get(
        "/api/v1/probation/roster", headers=auth_headers(settings, seed.outsider)
    )
    assert roster.status_code == 200
    assert [r["employee_name"] for r in roster.json()] == [seed.report.full_name]


async def test_an_outsider_cannot_tick_a_step(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _put_probationer(db, seed)

    res = await _set_step(client, settings, seed.outsider, seed.report, _HR_STEP)

    assert res.status_code == 404


# --- step ownership --------------------------------------------------------- #
async def test_hr_cannot_sign_the_employees_own_self_assessment(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The whole point of the column is accountability. If an admin can tick the
    employee's self-assessment, the record stops meaning anything."""
    await _put_probationer(db, seed)

    res = await _set_step(client, settings, seed.admin, seed.report, _EMPLOYEE_STEP)

    assert res.status_code == 403


async def test_the_employee_cannot_tick_an_hr_step(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _put_probationer(db, seed)

    res = await _set_step(client, settings, seed.report, seed.report, _HR_STEP)

    assert res.status_code == 403


async def test_the_employee_can_complete_their_own_step(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _put_probationer(db, seed)

    res = await _set_step(
        client, settings, seed.report, seed.report, _EMPLOYEE_STEP, note="Submitted"
    )

    assert res.status_code == 200
    step = next(s for s in res.json()["steps"] if s["no"] == _EMPLOYEE_STEP)
    assert step["status"] == "done"
    assert step["note"] == "Submitted"
    assert step["actor_name"] == seed.report.full_name
    assert step["acted_at"] is not None
    assert res.json()["settled_steps"] == 1


async def test_the_reporting_manager_can_complete_a_manager_step(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _put_probationer(db, seed)

    res = await _set_step(client, settings, seed.manager, seed.report, _MANAGER_STEP)

    assert res.status_code == 200
    step = next(s for s in res.json()["steps"] if s["no"] == _MANAGER_STEP)
    assert step["status"] == "done"
    assert step["actor_name"] == seed.manager.full_name


async def test_can_update_tells_the_ui_what_this_caller_may_move(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """So the UI disables a control rather than offering one the API will reject."""
    await _put_probationer(db, seed)

    body = (await _checklist(client, settings, seed.report, seed.report)).json()

    by_no = {s["no"]: s for s in body["steps"]}
    assert by_no[_EMPLOYEE_STEP]["can_update"] is True
    assert by_no[_HR_STEP]["can_update"] is False


# --- writes ----------------------------------------------------------------- #
async def test_skipped_counts_as_settled(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """ "Collect feedback from the CEO, if applicable" does not always apply, and
    forcing a tick would record something that never happened."""
    await _put_probationer(db, seed)

    res = await _set_step(client, settings, seed.admin, seed.report, 10, status="skipped")

    assert res.status_code == 200
    assert res.json()["settled_steps"] == 1


async def test_moving_a_step_back_to_pending_clears_the_signature(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Undo must not leave a stale name against a step nobody has completed."""
    await _put_probationer(db, seed)
    await _set_step(client, settings, seed.admin, seed.report, _HR_STEP)

    res = await _set_step(client, settings, seed.admin, seed.report, _HR_STEP, status="pending")

    assert res.status_code == 200
    step = next(s for s in res.json()["steps"] if s["no"] == _HR_STEP)
    assert step["status"] == "pending"
    assert step["actor_name"] is None
    assert step["acted_at"] is None
    assert res.json()["settled_steps"] == 0


async def test_ticking_the_same_step_twice_is_idempotent(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _put_probationer(db, seed)
    await _set_step(client, settings, seed.admin, seed.report, _HR_STEP)

    res = await _set_step(client, settings, seed.admin, seed.report, _HR_STEP)

    assert res.status_code == 200
    assert res.json()["settled_steps"] == 1


async def test_step_eleven_is_rejected(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """It is not part of the process any more, so it cannot be recorded."""
    await _put_probationer(db, seed)

    res = await _set_step(client, settings, seed.admin, seed.report, 11)

    assert res.status_code == 422

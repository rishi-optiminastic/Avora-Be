"""Sending a form to everyone, a department, or named people.

The audience is a CLAIM the server resolves against the live roster, never a
list the client supplies. That is the whole point: "everyone" has to mean
everyone active at the moment of sending, and a named list has to be checked.

Pinned here:
- each shape reaches exactly the right people, and only active ones;
- a re-send tops up (a late joiner) instead of handing anyone a second copy;
- a department nobody is in is a loud 422, not a silent send to no one;
- the audience cannot be used to reach someone who is not an employee.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers
from tests.test_forms import _create, _publish


async def _person(
    db: AsyncSession, *, name: str, department: str | None, active: bool = True
) -> Employee:
    person = Employee(
        hr_external_id=f"aud-{name.lower().replace(' ', '-')}",
        work_email=f"{name.lower().replace(' ', '.')}@corp.test",
        full_name=name,
        role=Role.EXECUTIVE,
        department=department,
        status=EmployeeStatus.ACTIVE if active else EmployeeStatus.INACTIVE,
        is_active=active,
    )
    db.add(person)
    await db.commit()
    return person


async def _live_form(client: AsyncClient, settings: Settings, admin: Employee) -> str:
    form = await _create(client, settings, admin)
    await _publish(client, settings, admin, form["id"])
    return str(form["id"])


async def _send(
    client: AsyncClient, settings: Settings, admin: Employee, form_id: str, audience: dict
):
    return await client.post(
        f"/api/v1/forms/{form_id}/assignments",
        json={"audience": audience},
        headers=auth_headers(settings, admin),
    )


def _names(payload: list[dict]) -> set[str]:
    return {str(row["assignee_name"]) for row in payload}


# --------------------------------------------------------------------------- #
# Everyone
# --------------------------------------------------------------------------- #
async def test_everyone_reaches_the_whole_active_roster(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _person(db, name="Dana Design", department="Design")
    await _person(db, name="Gone Person", department="Design", active=False)
    form_id = await _live_form(client, settings, seed.admin)

    res = await _send(client, settings, seed.admin, form_id, {"kind": "everyone"})

    assert res.status_code == 201, res.text
    names = _names(res.json())
    assert "Dana Design" in names
    assert "Gone Person" not in names  # left the company
    assert seed.report.full_name in names


# --------------------------------------------------------------------------- #
# Departments
# --------------------------------------------------------------------------- #
async def test_a_department_reaches_only_that_department(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _person(db, name="Dana Design", department="Design")
    await _person(db, name="Devi Design", department="design ")  # free text, same team
    await _person(db, name="Eli Eng", department="Engineering")
    form_id = await _live_form(client, settings, seed.admin)

    res = await _send(
        client,
        settings,
        seed.admin,
        form_id,
        {"kind": "departments", "departments": ["Design"]},
    )

    assert res.status_code == 201, res.text
    assert _names(res.json()) == {"Dana Design", "Devi Design"}


async def test_an_empty_department_is_refused_rather_than_sent_to_nobody(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A department renamed in HR is the usual cause; a silent zero-recipient
    send hides it until someone asks why nobody replied."""
    form_id = await _live_form(client, settings, seed.admin)

    res = await _send(
        client,
        settings,
        seed.admin,
        form_id,
        {"kind": "departments", "departments": ["Does Not Exist"]},
    )

    assert res.status_code == 422, res.text


async def test_a_departments_audience_needs_a_department(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form_id = await _live_form(client, settings, seed.admin)
    res = await _send(client, settings, seed.admin, form_id, {"kind": "departments"})
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# Named people
# --------------------------------------------------------------------------- #
async def test_named_people_are_validated_not_trusted(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form_id = await _live_form(client, settings, seed.admin)

    res = await _send(
        client,
        settings,
        seed.admin,
        form_id,
        {"kind": "people", "employee_ids": [str(seed.report.id), str(uuid.uuid4())]},
    )

    assert res.status_code == 422, res.text


async def test_a_people_audience_needs_somebody(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form_id = await _live_form(client, settings, seed.admin)
    res = await _send(client, settings, seed.admin, form_id, {"kind": "people"})
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# Re-sending
# --------------------------------------------------------------------------- #
async def test_resending_tops_up_instead_of_duplicating(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Sending to everyone twice is a normal thing to do - a late joiner, a
    nudge. Only the people who do not already hold it get one."""
    form_id = await _live_form(client, settings, seed.admin)
    first = await _send(client, settings, seed.admin, form_id, {"kind": "everyone"})
    assert first.status_code == 201
    before = len(first.json())

    await _person(db, name="Late Joiner", department="Design")
    second = await _send(client, settings, seed.admin, form_id, {"kind": "everyone"})

    assert second.status_code == 201, second.text
    assert _names(second.json()) == {"Late Joiner"}

    listed = await client.get(
        f"/api/v1/forms/{form_id}/assignments", headers=auth_headers(settings, seed.admin)
    )
    assert len(listed.json()) == before + 1


# --------------------------------------------------------------------------- #
# Who may send at all
# --------------------------------------------------------------------------- #
async def test_only_hr_and_admins_can_send_to_an_audience(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form_id = await _live_form(client, settings, seed.admin)
    for actor in (seed.report, seed.manager, seed.outsider):
        res = await _send(client, settings, actor, form_id, {"kind": "everyone"})
        assert res.status_code == 404, f"{actor.full_name}: {res.text}"

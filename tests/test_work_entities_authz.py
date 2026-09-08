"""Project-catalog authorization.

HR curates projects alongside Admin. Keeping the list current is day-to-day
people-ops, and gating it to Admin locked HR out of even READING the catalog, so
they could not see the projects their own team was assigned to, let alone add
one (be/CLAUDE.md 5.3: HR spans the org for people data).

Managers still may not curate it, but must be able to read the ACTIVE set, since
that is what they attach a task to.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

_URL = "/api/v1/work-entities"


async def _hr(db: AsyncSession) -> Employee:
    hr = Employee(
        hr_external_id="hr-projects",
        work_email="projects-hr@corp.test",
        full_name="Hilda HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    return hr


def _payload(**over: object) -> dict[str, object]:
    return {
        "name": "Bhiwandi Warehouse",
        "department": "Engineering",
        "keywords": ["warehouse", "bhiwandi"],
        **over,
    }


async def test_hr_can_create_list_update_and_delete_a_project(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    hr = await _hr(db)
    headers = auth_headers(settings, hr)

    created = await client.post(_URL, json=_payload(), headers=headers)
    assert created.status_code == 201, created.text
    entity_id = created.json()["id"]

    listed = await client.get(_URL, headers=headers)
    assert listed.status_code == 200, listed.text
    assert entity_id in [row["id"] for row in listed.json()]

    edited = await client.patch(
        f"{_URL}/{entity_id}", json={"name": "Bhiwandi Warehouse II"}, headers=headers
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["name"] == "Bhiwandi Warehouse II"

    removed = await client.delete(f"{_URL}/{entity_id}", headers=headers)
    assert removed.status_code == 204, removed.text


async def test_admin_keeps_full_access(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    headers = auth_headers(settings, seed.admin)
    created = await client.post(_URL, json=_payload(name="Admin Project"), headers=headers)
    assert created.status_code == 201, created.text
    assert (await client.get(_URL, headers=headers)).status_code == 200


async def test_nobody_else_may_curate_the_catalog(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A manager can attach work to a project but must not invent or delete one."""
    for actor in (seed.manager, seed.report, seed.outsider):
        who = actor.role
        assert (
            await client.post(_URL, json=_payload(), headers=auth_headers(settings, actor))
        ).status_code == 403, f"{who} created a project"
        assert (await client.get(_URL, headers=auth_headers(settings, actor))).status_code == 403, (
            f"{who} listed the catalog"
        )
        assert (
            await client.patch(
                f"{_URL}/{uuid.uuid4()}",
                json={"name": "x"},
                headers=auth_headers(settings, actor),
            )
        ).status_code == 403, f"{who} edited a project"
        assert (
            await client.delete(f"{_URL}/{uuid.uuid4()}", headers=auth_headers(settings, actor))
        ).status_code == 403, f"{who} deleted a project"


async def test_a_manager_can_still_read_the_active_picker(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Assigning a task means choosing a project, so the picker stays open to
    managers even though curating the catalog does not."""
    hr = await _hr(db)
    created = await client.post(_URL, json=_payload(), headers=auth_headers(settings, hr))
    assert created.status_code == 201

    for actor in (seed.manager, hr, seed.admin):
        picker = await client.get(f"{_URL}/active", headers=auth_headers(settings, actor))
        assert picker.status_code == 200, f"{actor.role} could not read the picker"
        assert created.json()["id"] in [row["id"] for row in picker.json()]

    # An individual contributor has no work to assign, so no picker.
    denied = await client.get(f"{_URL}/active", headers=auth_headers(settings, seed.report))
    assert denied.status_code == 403

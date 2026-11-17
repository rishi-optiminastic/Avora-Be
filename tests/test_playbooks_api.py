"""Playbook endpoints: who may author, who may run, and what a run reports.

A playbook creates work for OTHER people - the joiner, their manager, an HR
lead - so the authorization tests here are the point, not a formality. §9: an
endpoint without one is incomplete.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.task import Task
from tests.conftest import _Seed, auth_headers

URL = "/api/v1/playbooks"


def _book(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "New joiner",
        "description": "The eleven things that happen for every joiner.",
        "trigger": "manual",
        "steps": [
            {"title_template": "Induction - {name}", "offset_days": 0},
            {
                "title_template": "Manager check-in with {first_name}",
                "assignee_rule": "subject_manager",
                "offset_days": 3,
            },
        ],
    }
    body.update(over)
    return body


async def _joiner(db: AsyncSession, manager_id: uuid.UUID | None = None) -> Employee:
    person = Employee(
        hr_external_id="hr-pb-api",
        work_email="aditi.api@corp.test",
        full_name="Aditi Sharma",
        role=Role.INTERN,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
        department="Social Media",
        job_title="SM Intern",
        manager_id=manager_id,
    )
    db.add(person)
    await db.flush()
    return person


async def _published(client: AsyncClient, settings: Settings, actor: Employee) -> str:
    head = auth_headers(settings, actor)
    made = await client.post(URL, json=_book(), headers=head)
    assert made.status_code == 201, made.text
    pid = made.json()["id"]
    live = await client.post(f"{URL}/{pid}/publish", headers=head)
    assert live.status_code == 200, live.text
    return str(pid)


# --------------------------------------------------------------------------- #
# Who may do what
# --------------------------------------------------------------------------- #
async def test_every_endpoint_requires_authentication(client: AsyncClient, seed: _Seed) -> None:
    for method, path in (
        ("get", URL),
        ("post", URL),
        ("get", f"{URL}/{uuid.uuid4()}"),
        ("put", f"{URL}/{uuid.uuid4()}"),
        ("post", f"{URL}/{uuid.uuid4()}/publish"),
        ("post", f"{URL}/{uuid.uuid4()}/archive"),
        ("post", f"{URL}/{uuid.uuid4()}/run"),
        ("post", f"{URL}/runs/{uuid.uuid4()}/cancel"),
        ("get", f"{URL}/runs/employee/{uuid.uuid4()}"),
    ):
        kwargs = {"json": {}} if method in ("post", "put") else {}
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 401, f"{method} {path}"


async def test_only_hr_and_admins_author_a_playbook(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.post(URL, json=_book(), headers=auth_headers(settings, actor))
        assert resp.status_code == 403, f"{actor.full_name}: {resp.text}"


async def test_only_hr_and_admins_run_a_playbook(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    # A manager must NOT be able to: a run creates tasks for the HR lead and
    # for the joiner, well outside any one manager's scope.
    pid = await _published(client, settings, seed.admin)
    joiner = await _joiner(db)
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.post(
            f"{URL}/{pid}/run",
            json={"subject_id": str(joiner.id)},
            headers=auth_headers(settings, actor),
        )
        assert resp.status_code == 403, f"{actor.full_name}: {resp.text}"
    assert await db.scalar(select(func.count()).select_from(Task)) == 0


async def test_only_hr_and_admins_list_playbooks(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.get(URL, headers=auth_headers(settings, actor))
        assert resp.status_code == 403, f"{actor.full_name}: {resp.text}"


async def test_a_person_may_read_their_own_run_history(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Why eleven tasks appeared on your first morning is your business.
    resp = await client.get(
        f"{URL}/runs/employee/{seed.report.id}", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 200, resp.text


async def test_a_person_may_not_read_somebody_elses_run_history(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get(
        f"{URL}/runs/employee/{seed.manager.id}", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# Behaviour
# --------------------------------------------------------------------------- #
async def test_a_run_creates_the_tasks_and_reports_what_it_skipped(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    pid = await _published(client, settings, seed.admin)
    joiner = await _joiner(db)  # no manager on file

    resp = await client.post(
        f"{URL}/{pid}/run",
        json={"subject_id": str(joiner.id)},
        headers=auth_headers(settings, seed.admin),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 1
    assert len(body["skipped"]) == 1, "the manager step has nobody to go to and must say so"
    titles = (await db.scalars(select(Task.title))).all()
    assert list(titles) == ["Induction - Aditi Sharma"]


async def test_a_draft_cannot_be_run(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    head = auth_headers(settings, seed.admin)
    made = await client.post(URL, json=_book(), headers=head)
    pid = made.json()["id"]
    joiner = await _joiner(db)

    resp = await client.post(f"{URL}/{pid}/run", json={"subject_id": str(joiner.id)}, headers=head)

    # 409, not 500: the request is fine and the caller is allowed - the
    # playbook simply is not ready.
    assert resp.status_code == 409, resp.text
    assert await db.scalar(select(func.count()).select_from(Task)) == 0


async def test_publishing_rejects_a_placeholder_nothing_can_fill(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    head = auth_headers(settings, seed.admin)
    resp = await client.post(
        URL,
        json=_book(steps=[{"title_template": "Induction - {nmae}"}]),
        headers=head,
    )
    # Caught at the edge, before it can ever reach a joiner's task title.
    assert resp.status_code == 422, resp.text


async def test_an_automatic_playbook_is_armed_from_today_not_backwards(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # THE backfill guard. Without `auto_assign_since`, publishing would fire
    # for everybody already on the roster.
    head = auth_headers(settings, seed.admin)
    made = await client.post(URL, json=_book(trigger="new_joiner", trigger_days=0), headers=head)
    pid = made.json()["id"]
    assert made.json()["auto_assign_since"] is None, "a draft arms nothing"

    live = await client.post(f"{URL}/{pid}/publish", headers=head)

    assert live.status_code == 200, live.text
    assert live.json()["auto_assign_since"] is not None, "publishing must arm it from a date"


async def test_cancelling_a_run_removes_its_untouched_tasks(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    pid = await _published(client, settings, seed.admin)
    joiner = await _joiner(db, manager_id=seed.manager.id)
    head = auth_headers(settings, seed.admin)
    run = await client.post(f"{URL}/{pid}/run", json={"subject_id": str(joiner.id)}, headers=head)
    assert run.json()["created"] == 2

    resp = await client.post(f"{URL}/runs/{run.json()['run_id']}/cancel", headers=head)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"tasks_removed": 2, "tasks_kept": 0}
    assert await db.scalar(select(func.count()).select_from(Task)) == 0


async def test_only_hr_and_admins_cancel_a_run(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    pid = await _published(client, settings, seed.admin)
    joiner = await _joiner(db)
    head = auth_headers(settings, seed.admin)
    run = await client.post(f"{URL}/{pid}/run", json={"subject_id": str(joiner.id)}, headers=head)
    rid = run.json()["run_id"]

    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.post(f"{URL}/runs/{rid}/cancel", headers=auth_headers(settings, actor))
        assert resp.status_code == 403, f"{actor.full_name}: {resp.text}"

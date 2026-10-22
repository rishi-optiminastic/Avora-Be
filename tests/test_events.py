"""Events: what is on, who it is for, and the form that collects answers.

HR and admins run them; everyone else sees the published events they are in the
audience for. There is deliberately no separate RSVP store - attaching a form
gives sign-ups and feedback through the one answer table.

Pinned here:
- a draft is invisible to the audience, a published event is not, and a
  cancelled one stays visible (people planned around it);
- the audience decides visibility, per kind;
- publishing sends the attached form to that same audience, exactly once,
  however many times publish is pressed;
- a published event is cancelled, never deleted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers
from tests.test_forms import _create, _publish

_WHEN = (datetime.now(UTC) + timedelta(days=14)).replace(microsecond=0)


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "title": "Quarterly town hall",
        "description": "Numbers, roadmap, questions.",
        "location": "HQ · Mumbai",
        "starts_at": _WHEN.isoformat(),
        "ends_at": (_WHEN + timedelta(hours=2)).isoformat(),
        "audience": {"kind": "everyone"},
    }
    body.update(overrides)
    return body


def _instant(value: object) -> datetime:
    """Parse a timestamp from the API, treating a naive one as UTC.

    SQLite drops tzinfo on the way back out, so a value just written in Python
    comes back as "...Z" while the same value re-read comes back naive. Postgres
    keeps the offset; comparing instants rather than strings makes the test mean
    the same thing on both.
    """
    assert isinstance(value, str)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _person(db: AsyncSession, *, name: str, department: str | None) -> Employee:
    person = Employee(
        hr_external_id=f"ev-{name.lower().replace(' ', '-')}",
        work_email=f"{name.lower().replace(' ', '.')}@corp.test",
        full_name=name,
        role=Role.EXECUTIVE,
        department=department,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(person)
    await db.commit()
    return person


async def _create_event(
    client: AsyncClient, settings: Settings, who: Employee, **overrides: object
):
    return await client.post(
        "/api/v1/events", json=_body(**overrides), headers=auth_headers(settings, who)
    )


async def _publish_event(client: AsyncClient, settings: Settings, who: Employee, event_id: str):
    return await client.post(
        f"/api/v1/events/{event_id}/publish", headers=auth_headers(settings, who)
    )


async def _visible_to(client: AsyncClient, settings: Settings, who: Employee) -> list[dict]:
    res = await client.get("/api/v1/events", headers=auth_headers(settings, who))
    assert res.status_code == 200, res.text
    return list(res.json())


# --------------------------------------------------------------------------- #
# Who may run them
# --------------------------------------------------------------------------- #
async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/events")).status_code == 401


async def test_only_hr_and_admins_create_events(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager, seed.outsider):
        res = await _create_event(client, settings, actor)
        assert res.status_code == 403, f"{actor.full_name}: {res.text}"
    assert (await _create_event(client, settings, seed.admin)).status_code == 201


# --------------------------------------------------------------------------- #
# Visibility
# --------------------------------------------------------------------------- #
async def test_a_draft_is_invisible_to_the_audience(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """An unfinished event is not an announcement."""
    created = await _create_event(client, settings, seed.admin)
    event_id = created.json()["id"]

    assert _visible_to(client, settings, seed.report) is not None
    assert not [e for e in await _visible_to(client, settings, seed.report) if e["id"] == event_id]
    detail = await client.get(
        f"/api/v1/events/{event_id}", headers=auth_headers(settings, seed.report)
    )
    assert detail.status_code == 404


async def test_publishing_makes_it_visible(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await _create_event(client, settings, seed.admin)
    event_id = created.json()["id"]

    published = await _publish_event(client, settings, seed.admin, event_id)

    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    mine = await _visible_to(client, settings, seed.report)
    assert [e for e in mine if e["id"] == event_id]


async def test_a_department_event_reaches_only_that_department(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    designer = await _person(db, name="Dana Design", department="Design")
    engineer = await _person(db, name="Eli Eng", department="Engineering")
    created = await _create_event(
        client,
        settings,
        seed.admin,
        audience={"kind": "departments", "departments": ["Design"]},
    )
    event_id = created.json()["id"]
    await _publish_event(client, settings, seed.admin, event_id)

    assert [e for e in await _visible_to(client, settings, designer) if e["id"] == event_id]
    assert not [e for e in await _visible_to(client, settings, engineer) if e["id"] == event_id]


async def test_a_named_people_event_reaches_only_them(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await _create_event(
        client,
        settings,
        seed.admin,
        audience={"kind": "people", "employee_ids": [str(seed.report.id)]},
    )
    event_id = created.json()["id"]
    await _publish_event(client, settings, seed.admin, event_id)

    assert [e for e in await _visible_to(client, settings, seed.report) if e["id"] == event_id]
    assert not [
        e for e in await _visible_to(client, settings, seed.outsider) if e["id"] == event_id
    ]


async def test_the_audience_size_is_reported(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Computed at read time, not stored: "everyone" written today has to mean
    the roster next month."""
    created = await _create_event(client, settings, seed.admin)
    before = created.json()["audience"]["size"]

    await _person(db, name="New Start", department="Design")
    again = await client.get(
        f"/api/v1/events/{created.json()['id']}", headers=auth_headers(settings, seed.admin)
    )

    assert again.json()["audience"]["size"] == before + 1


# --------------------------------------------------------------------------- #
# The attached form
# --------------------------------------------------------------------------- #
async def test_publishing_sends_the_form_to_the_audience_once(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form = await _create(client, settings, seed.admin)
    await _publish(client, settings, seed.admin, form["id"])
    created = await _create_event(client, settings, seed.admin, form_id=form["id"])
    event_id = created.json()["id"]

    first = await _publish_event(client, settings, seed.admin, event_id)
    assert first.status_code == 200, first.text
    sent_at = first.json()["form_sent_at"]
    assert sent_at is not None

    listed = await client.get(
        f"/api/v1/forms/{form['id']}/assignments", headers=auth_headers(settings, seed.admin)
    )
    count = len(listed.json())
    assert count > 0

    # Publishing again must not hand anyone a second copy.
    second = await _publish_event(client, settings, seed.admin, event_id)
    assert _instant(second.json()["form_sent_at"]) == _instant(sent_at)
    listed_again = await client.get(
        f"/api/v1/forms/{form['id']}/assignments", headers=auth_headers(settings, seed.admin)
    )
    assert len(listed_again.json()) == count


async def test_the_audience_gets_a_link_to_their_own_copy(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form = await _create(client, settings, seed.admin)
    await _publish(client, settings, seed.admin, form["id"])
    created = await _create_event(
        client,
        settings,
        seed.admin,
        form_id=form["id"],
        audience={"kind": "people", "employee_ids": [str(seed.report.id)]},
    )
    event_id = created.json()["id"]
    await _publish_event(client, settings, seed.admin, event_id)

    mine = await client.get(
        f"/api/v1/events/{event_id}", headers=auth_headers(settings, seed.report)
    )

    assert mine.status_code == 200, mine.text
    assert mine.json()["my_assignment_id"] is not None
    # Reply counts are an organiser's business, not the audience's.
    assert mine.json()["form_submitted"] is None


async def test_only_a_published_form_can_be_attached(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    draft = await _create(client, settings, seed.admin)
    res = await _create_event(client, settings, seed.admin, form_id=draft["id"])
    assert res.status_code == 422, res.text


async def test_an_unknown_form_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await _create_event(client, settings, seed.admin, form_id=str(uuid.uuid4()))
    assert res.status_code == 422


async def test_a_sent_form_cannot_be_swapped(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """It is already in people's lists; swapping it would leave them holding a
    form for an event that no longer uses it."""
    form = await _create(client, settings, seed.admin)
    await _publish(client, settings, seed.admin, form["id"])
    other = await _create(client, settings, seed.admin, title="Something else")
    await _publish(client, settings, seed.admin, other["id"])
    created = await _create_event(client, settings, seed.admin, form_id=form["id"])
    event_id = created.json()["id"]
    await _publish_event(client, settings, seed.admin, event_id)

    res = await client.put(
        f"/api/v1/events/{event_id}",
        json=_body(form_id=other["id"]),
        headers=auth_headers(settings, seed.admin),
    )

    assert res.status_code == 409, res.text


# --------------------------------------------------------------------------- #
# Editing, cancelling, deleting
# --------------------------------------------------------------------------- #
async def test_an_event_cannot_end_before_it_starts(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await _create_event(
        client, settings, seed.admin, ends_at=(_WHEN - timedelta(hours=1)).isoformat()
    )
    assert res.status_code == 422


async def test_cancelling_keeps_it_visible_to_the_audience(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """People who planned around it need to see it is off, not find it gone."""
    created = await _create_event(client, settings, seed.admin)
    event_id = created.json()["id"]
    await _publish_event(client, settings, seed.admin, event_id)

    cancelled = await client.post(
        f"/api/v1/events/{event_id}/cancel", headers=auth_headers(settings, seed.admin)
    )

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    theirs = [e for e in await _visible_to(client, settings, seed.report) if e["id"] == event_id]
    assert theirs and theirs[0]["status"] == "cancelled"


async def test_a_cancelled_event_cannot_be_edited_or_republished(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await _create_event(client, settings, seed.admin)
    event_id = created.json()["id"]
    await _publish_event(client, settings, seed.admin, event_id)
    await client.post(
        f"/api/v1/events/{event_id}/cancel", headers=auth_headers(settings, seed.admin)
    )

    edited = await client.put(
        f"/api/v1/events/{event_id}",
        json=_body(title="Back on"),
        headers=auth_headers(settings, seed.admin),
    )
    republished = await _publish_event(client, settings, seed.admin, event_id)

    assert edited.status_code == 409
    assert republished.status_code == 409


async def test_a_draft_can_be_deleted_but_a_published_event_cannot(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    draft = (await _create_event(client, settings, seed.admin)).json()
    live = (await _create_event(client, settings, seed.admin)).json()
    await _publish_event(client, settings, seed.admin, live["id"])

    gone = await client.delete(
        f"/api/v1/events/{draft['id']}", headers=auth_headers(settings, seed.admin)
    )
    kept = await client.delete(
        f"/api/v1/events/{live['id']}", headers=auth_headers(settings, seed.admin)
    )

    assert gone.status_code == 204
    assert kept.status_code == 409


async def test_ordinary_people_cannot_change_an_event(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await _create_event(client, settings, seed.admin)
    event_id = created.json()["id"]
    await _publish_event(client, settings, seed.admin, event_id)
    headers = auth_headers(settings, seed.report)

    assert (
        await client.put(f"/api/v1/events/{event_id}", json=_body(), headers=headers)
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/events/{event_id}/publish", headers=headers)
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/events/{event_id}/cancel", headers=headers)
    ).status_code == 403
    assert (await client.delete(f"/api/v1/events/{event_id}", headers=headers)).status_code == 403

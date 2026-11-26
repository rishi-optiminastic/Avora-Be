"""Linking a task to a target.

The interesting part is not the column, it is who may point at what. A target
carries somebody's number, so being able to attach a task to one by id would
be a way to confirm that target exists - which is why an out-of-scope target
is reported as unknown rather than forbidden, and why that has a test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee
from app.models.target import Target
from tests.conftest import _Seed, auth_headers

TASKS = "/api/v1/tasks"


def _task(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Draft the Q1 pricing deck",
        "due_date": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
    }
    payload.update(overrides)
    return payload


async def _make_target(db: AsyncSession, owner: Employee, title: str) -> Target:
    target = Target(
        title=title,
        owner_id=owner.id,
        metric="Revenue",
        unit="INR",
        target_value=1_000_000.0,
    )
    db.add(target)
    await db.commit()
    await db.refresh(target)
    return target


@pytest_asyncio.fixture
async def mine(db: AsyncSession, seed: _Seed) -> Target:
    """A target owned by the manager, who can therefore see it."""
    return await _make_target(db, seed.manager, "Grow enterprise revenue")


@pytest_asyncio.fixture
async def theirs(db: AsyncSession, seed: _Seed) -> Target:
    """A target owned by somebody outside the manager's scope."""
    return await _make_target(db, seed.outsider, "Somebody else's number")


# --------------------------------------------------------------------------- #
# Authorization (be/CLAUDE.md section 9 - not optional)
# --------------------------------------------------------------------------- #
async def test_a_task_cannot_be_linked_to_a_target_out_of_scope(
    client: AsyncClient, settings: Settings, seed: _Seed, theirs: Target
) -> None:
    """Reported as unknown, not forbidden: "you may not touch that" confirms it
    exists, which is the thing being withheld."""
    resp = await client.post(
        TASKS,
        json=_task(assignee_id=str(seed.manager.id), target_id=str(theirs.id)),
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 422, resp.text
    assert "Unknown target" in resp.text


async def test_a_task_cannot_be_RE_linked_to_a_target_out_of_scope(
    client: AsyncClient, settings: Settings, seed: _Seed, mine: Target, theirs: Target
) -> None:
    """The update path needs its own check - a create guard that an edit skips
    is not a guard."""
    created = await client.post(
        TASKS,
        json=_task(assignee_id=str(seed.manager.id), target_id=str(mine.id)),
        headers=auth_headers(settings, seed.manager),
    )
    assert created.status_code == 201, created.text

    moved = await client.patch(
        f"{TASKS}/{created.json()['id']}",
        json={"target_id": str(theirs.id)},
        headers=auth_headers(settings, seed.manager),
    )
    assert moved.status_code == 422, moved.text


async def test_a_made_up_target_id_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        TASKS,
        json=_task(assignee_id=str(seed.manager.id), target_id=str(uuid.uuid4())),
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# The link itself
# --------------------------------------------------------------------------- #
async def test_a_task_carries_its_target_and_its_title(
    client: AsyncClient, settings: Settings, seed: _Seed, mine: Target
) -> None:
    """The title rides along so a list of tasks can name the goal each serves
    without a request per row."""
    resp = await client.post(
        TASKS,
        json=_task(assignee_id=str(seed.manager.id), target_id=str(mine.id)),
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["target_id"] == str(mine.id)
    assert body["target_title"] == "Grow enterprise revenue"


async def test_the_target_title_follows_a_re_link(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession, mine: Target
) -> None:
    """The selectin relation holds the OLD target until refreshed, so without
    the reload the response names the goal the task used to serve."""
    other = await _make_target(db, seed.manager, "Cut churn")
    created = await client.post(
        TASKS,
        json=_task(assignee_id=str(seed.manager.id), target_id=str(mine.id)),
        headers=auth_headers(settings, seed.manager),
    )
    assert created.status_code == 201, created.text

    moved = await client.patch(
        f"{TASKS}/{created.json()['id']}",
        json={"target_id": str(other.id)},
        headers=auth_headers(settings, seed.manager),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["target_title"] == "Cut churn"


async def test_a_task_can_be_unlinked(
    client: AsyncClient, settings: Settings, seed: _Seed, mine: Target
) -> None:
    created = await client.post(
        TASKS,
        json=_task(assignee_id=str(seed.manager.id), target_id=str(mine.id)),
        headers=auth_headers(settings, seed.manager),
    )
    cleared = await client.patch(
        f"{TASKS}/{created.json()['id']}",
        json={"target_id": None},
        headers=auth_headers(settings, seed.manager),
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["target_id"] is None
    assert cleared.json()["target_title"] is None


async def test_tasks_can_be_listed_for_one_target(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession, mine: Target
) -> None:
    """What the target page asks: which work is going towards this number."""
    other = await _make_target(db, seed.manager, "Cut churn")
    for target, title in ((mine, "On the revenue goal"), (other, "On the churn goal")):
        resp = await client.post(
            TASKS,
            json=_task(title=title, assignee_id=str(seed.manager.id), target_id=str(target.id)),
            headers=auth_headers(settings, seed.manager),
        )
        assert resp.status_code == 201, resp.text

    listed = await client.get(
        f"{TASKS}?target_id={mine.id}", headers=auth_headers(settings, seed.manager)
    )
    assert listed.status_code == 200, listed.text
    titles = [row["title"] for row in listed.json()["items"]]
    assert titles == ["On the revenue goal"]

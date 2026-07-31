"""Task discussion-thread authorization — the thread is scoped to the task's
viewers (Testing §9). Anyone who can see the task can read + post; an out-of-scope
user gets 404 (never leaking that the task exists), mirroring the leave thread.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


async def _create_task(client: AsyncClient, settings: Settings, seed: _Seed) -> str:
    resp = await client.post(
        "/api/v1/tasks",
        json={"title": "Ship it", "assignee_id": str(seed.report.id), "cadence": "daily"},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _titles(client: AsyncClient, settings: Settings, actor: object) -> list[str]:
    resp = await client.get("/api/v1/notifications", headers=auth_headers(settings, actor))
    assert resp.status_code == 200
    return [n["title"] for n in resp.json()["items"]]


async def test_comment_notifies_other_participants_not_the_author(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task_id = await _create_task(client, settings, seed)  # manager -> report

    # The report (assignee) posts a message.
    posted = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"body": "Quick update for the team."},
        headers=auth_headers(settings, seed.report),
    )
    assert posted.status_code == 201, posted.text

    # The manager (assigner) is notified; the author is NOT notified of their own.
    assert any("New message on" in t for t in await _titles(client, settings, seed.manager))
    assert not any("New message on" in t for t in await _titles(client, settings, seed.report))


async def test_removing_a_collaborator_notifies_them(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task_id = await _create_task(client, settings, seed)
    admin = auth_headers(settings, seed.admin)

    added = await client.post(
        f"/api/v1/tasks/{task_id}/collaborators",
        json={"employee_id": str(seed.outsider.id)},
        headers=admin,
    )
    assert added.status_code == 200, added.text
    removed = await client.delete(
        f"/api/v1/tasks/{task_id}/collaborators/{seed.outsider.id}", headers=admin
    )
    assert removed.status_code == 200, removed.text

    assert any("Removed from a task" in t for t in await _titles(client, settings, seed.outsider))


async def test_assignee_and_manager_can_discuss(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task_id = await _create_task(client, settings, seed)

    # The assignee posts a message.
    posted = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"body": "On it — starting today."},
        headers=auth_headers(settings, seed.report),
    )
    assert posted.status_code == 201, posted.text
    assert posted.json()["author_id"] == str(seed.report.id)

    # The manager replies.
    reply = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"body": "Great, ping me if blocked."},
        headers=auth_headers(settings, seed.manager),
    )
    assert reply.status_code == 201

    # Both messages are in the thread, oldest-first.
    listed = await client.get(
        f"/api/v1/tasks/{task_id}/comments", headers=auth_headers(settings, seed.report)
    )
    assert listed.status_code == 200
    bodies = [c["body"] for c in listed.json()]
    assert bodies == ["On it — starting today.", "Great, ping me if blocked."]


async def test_replayed_comment_is_deduped(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A copied/looped request posting the same text returns the first comment
    (idempotent) instead of filling the thread with duplicates."""
    task_id = await _create_task(client, settings, seed)
    headers = auth_headers(settings, seed.report)
    body = {"body": "yo"}

    first = await client.post(f"/api/v1/tasks/{task_id}/comments", json=body, headers=headers)
    assert first.status_code == 201
    first_id = first.json()["id"]

    # Replay the exact same request several times — none create a new row.
    for _ in range(5):
        again = await client.post(f"/api/v1/tasks/{task_id}/comments", json=body, headers=headers)
        assert again.status_code == 201
        assert again.json()["id"] == first_id

    listed = await client.get(f"/api/v1/tasks/{task_id}/comments", headers=headers)
    assert [c["body"] for c in listed.json()] == ["yo"]

    # A genuinely different message still posts.
    other = await client.post(
        f"/api/v1/tasks/{task_id}/comments", json={"body": "done"}, headers=headers
    )
    assert other.status_code == 201
    assert other.json()["id"] != first_id


async def test_same_idempotency_key_returns_original(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A copied request carries the same Idempotency-Key, so replaying it returns
    the original comment — even hours later, unlike the content time-window."""
    task_id = await _create_task(client, settings, seed)
    headers = {**auth_headers(settings, seed.report), "Idempotency-Key": "key-abc-123"}

    first = await client.post(
        f"/api/v1/tasks/{task_id}/comments", json={"body": "yo"}, headers=headers
    )
    assert first.status_code == 201
    first_id = first.json()["id"]

    for _ in range(5):
        again = await client.post(
            f"/api/v1/tasks/{task_id}/comments", json={"body": "yo"}, headers=headers
        )
        assert again.status_code == 201
        assert again.json()["id"] == first_id

    # A new key (what the real client sends per send) posts a genuine new "yo".
    fresh = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"body": "yo"},
        headers={**auth_headers(settings, seed.report), "Idempotency-Key": "key-def-456"},
    )
    assert fresh.status_code == 201
    assert fresh.json()["id"] != first_id

    listed = await client.get(
        f"/api/v1/tasks/{task_id}/comments", headers=auth_headers(settings, seed.report)
    )
    assert [c["body"] for c in listed.json()] == ["yo", "yo"]


async def test_one_users_key_does_not_collide_with_another(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Keys are scoped per author — the manager reusing the report's key still posts."""
    task_id = await _create_task(client, settings, seed)
    key = {"Idempotency-Key": "shared-key"}

    a = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"body": "from report"},
        headers={**auth_headers(settings, seed.report), **key},
    )
    b = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"body": "from manager"},
        headers={**auth_headers(settings, seed.manager), **key},
    )
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


async def test_outsider_cannot_read_thread(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task_id = await _create_task(client, settings, seed)
    resp = await client.get(
        f"/api/v1/tasks/{task_id}/comments", headers=auth_headers(settings, seed.outsider)
    )
    assert resp.status_code == 404


async def test_outsider_cannot_post(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    task_id = await _create_task(client, settings, seed)
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"body": "I shouldn't be able to say this."},
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 404


async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    # A random uuid is fine — auth is checked before the task lookup.
    fake = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"/api/v1/tasks/{fake}/comments")).status_code == 401


async def test_empty_body_rejected(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    task_id = await _create_task(client, settings, seed)
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"body": ""},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422

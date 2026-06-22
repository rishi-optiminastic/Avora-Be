"""Notification authz: an inbox is private to its recipient (§9).

Uses the real emission path — when the report applies for leave, their manager
(the reviewer) gets a LEAVE_REQUEST notification — then proves only that manager
can see and act on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


def _apply_body() -> dict[str, object]:
    start = datetime.now(UTC) + timedelta(days=3)
    return {
        "leave_type": "planned",
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=1)).isoformat(),
        "reason": "Trip",
    }


async def _notify_manager(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    resp = await client.post(
        "/api/v1/leaves", json=_apply_body(), headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 201, resp.text


async def test_list_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/notifications")).status_code == 401


async def test_recipient_sees_and_others_dont(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _notify_manager(client, settings, seed)

    # The manager (reviewer) has the leave-request notification.
    mine = await client.get("/api/v1/notifications", headers=auth_headers(settings, seed.manager))
    assert mine.status_code == 200
    items = mine.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "leave_request"

    count = await client.get(
        "/api/v1/notifications/unread-count", headers=auth_headers(settings, seed.manager)
    )
    assert count.json()["count"] == 1

    # An unrelated employee sees nothing — inboxes are private.
    theirs = await client.get(
        "/api/v1/notifications", headers=auth_headers(settings, seed.outsider)
    )
    assert theirs.json()["items"] == []
    other_count = await client.get(
        "/api/v1/notifications/unread-count", headers=auth_headers(settings, seed.outsider)
    )
    assert other_count.json()["count"] == 0


async def test_outsider_cannot_read_anothers_notification(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _notify_manager(client, settings, seed)
    notif_id = (
        await client.get("/api/v1/notifications", headers=auth_headers(settings, seed.manager))
    ).json()["items"][0]["id"]

    # Marking someone else's notification read is a scoped no-op (204, no effect).
    resp = await client.post(
        f"/api/v1/notifications/{notif_id}/read",
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 204

    still_unread = await client.get(
        "/api/v1/notifications/unread-count", headers=auth_headers(settings, seed.manager)
    )
    assert still_unread.json()["count"] == 1


async def test_recipient_marks_read(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    await _notify_manager(client, settings, seed)
    notif_id = (
        await client.get("/api/v1/notifications", headers=auth_headers(settings, seed.manager))
    ).json()["items"][0]["id"]

    resp = await client.post(
        f"/api/v1/notifications/{notif_id}/read", headers=auth_headers(settings, seed.manager)
    )
    assert resp.status_code == 204

    count = await client.get(
        "/api/v1/notifications/unread-count", headers=auth_headers(settings, seed.manager)
    )
    assert count.json()["count"] == 0

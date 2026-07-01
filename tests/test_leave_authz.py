"""Leave authorization tests (§9: every protected endpoint has an authz test)."""

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


async def _apply_as(client: AsyncClient, settings: Settings, actor) -> dict[str, object]:
    resp = await client.post(
        "/api/v1/leaves", json=_apply_body(), headers=auth_headers(settings, actor)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/leaves")).status_code == 401


async def test_employee_applies_sets_manager_as_reviewer(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave = await _apply_as(client, settings, seed.report)
    assert leave["employee_id"] == str(seed.report.id)
    assert leave["reviewer_id"] == str(seed.manager.id)
    assert leave["status"] == "submitted"


async def test_manager_sees_report_request_outsider_does_not(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _apply_as(client, settings, seed.report)
    mgr = await client.get("/api/v1/leaves", headers=auth_headers(settings, seed.manager))
    assert mgr.json()["total"] == 1
    out = await client.get("/api/v1/leaves", headers=auth_headers(settings, seed.outsider))
    assert out.json()["total"] == 0


async def test_admin_approves_manager_cannot(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave = await _apply_as(client, settings, seed.report)
    # A manager can SEE the request (repo scope) but MUST NOT approve it —
    # approval is admin-only.
    denied = await client.post(
        f"/api/v1/leaves/{leave['id']}/decision",
        json={"approve": True, "note": "ok"},
        headers=auth_headers(settings, seed.manager),
    )
    assert denied.status_code == 403

    # The admin approves.
    resp = await client.post(
        f"/api/v1/leaves/{leave['id']}/decision",
        json={"approve": True, "note": "ok"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["reviewer_id"] == str(seed.admin.id)


async def test_requester_cannot_approve_own(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave = await _apply_as(client, settings, seed.report)
    resp = await client.post(
        f"/api/v1/leaves/{leave['id']}/decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 403


async def test_requester_can_withdraw(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    leave = await _apply_as(client, settings, seed.report)
    resp = await client.post(
        f"/api/v1/leaves/{leave['id']}/withdraw", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "withdrawn"


async def test_requester_and_reviewer_can_comment(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave = await _apply_as(client, settings, seed.report)
    lid = leave["id"]

    posted = await client.post(
        f"/api/v1/leaves/{lid}/comments",
        json={"body": "Can I move this a day?"},
        headers=auth_headers(settings, seed.report),
    )
    assert posted.status_code == 201
    assert posted.json()["author_id"] == str(seed.report.id)

    # The manager (reviewer) can also post and read the thread.
    await client.post(
        f"/api/v1/leaves/{lid}/comments",
        json={"body": "Sure — go ahead."},
        headers=auth_headers(settings, seed.manager),
    )
    thread = await client.get(
        f"/api/v1/leaves/{lid}/comments", headers=auth_headers(settings, seed.manager)
    )
    assert thread.status_code == 200
    assert len(thread.json()) == 2


async def test_comment_posting_is_rate_limited(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Admin applies for their own leave, then spams comments past the per-user cap.
    leave = await _apply_as(client, settings, seed.admin)
    lid = leave["id"]
    statuses = []
    for i in range(17):
        resp = await client.post(
            f"/api/v1/leaves/{lid}/comments",
            json={"body": f"msg {i}"},
            headers=auth_headers(settings, seed.admin),
        )
        statuses.append(resp.status_code)
    # The cap is 15/min — at least one later attempt is throttled with 429.
    assert 429 in statuses
    assert statuses[-1] == 429


async def test_outsider_cannot_read_or_post_comments(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave = await _apply_as(client, settings, seed.report)
    lid = leave["id"]
    assert (
        await client.get(
            f"/api/v1/leaves/{lid}/comments", headers=auth_headers(settings, seed.outsider)
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/leaves/{lid}/comments",
            json={"body": "snoop"},
            headers=auth_headers(settings, seed.outsider),
        )
    ).status_code == 404

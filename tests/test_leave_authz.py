"""Leave authorization tests (§9: every protected endpoint has an authz test)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from tests.conftest import _FakeEmailService, _Seed, auth_headers


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


async def test_planned_leave_needs_advance_notice(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Planned leave starting tomorrow is under the 2-day default notice → rejected.
    start = datetime.now(UTC) + timedelta(days=1)
    body = {
        "leave_type": "planned",
        "start_date": start.isoformat(),
        "end_date": start.isoformat(),
        "reason": "Trip",
    }
    resp = await client.post(
        "/api/v1/leaves", json=body, headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 422


async def test_sick_leave_can_be_applied_same_day(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Sick leave bypasses the notice rule — appliable for today.
    today = datetime.now(UTC)
    body = {
        "leave_type": "sick",
        "start_date": today.isoformat(),
        "end_date": today.isoformat(),
        "reason": "Unwell",
    }
    resp = await client.post(
        "/api/v1/leaves", json=body, headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 201, resp.text


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


async def test_an_admin_who_is_not_their_manager_cannot_approve(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Admin used to approve anybody's leave. It no longer does: a request goes
    to the person you report to, and an admin who is not that person cannot even
    see it (org decision - see the visibility tests below)."""
    leave = await _apply_as(client, settings, seed.report)  # reports to seed.manager

    resp = await client.post(
        f"/api/v1/leaves/{leave['id']}/decision",
        json={"approve": True, "note": "ok"},
        headers=auth_headers(settings, seed.admin),
    )

    assert resp.status_code in (403, 404)


async def test_the_reporting_manager_approves_their_own_reports(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Approval used to be admin-only, which left a manager looking at their own
    team's requests with no way to action them and every decision queued behind
    one person."""
    leave = await _apply_as(client, settings, seed.report)

    resp = await client.post(
        f"/api/v1/leaves/{leave['id']}/decision",
        json={"approve": True, "note": "covered"},
        headers=auth_headers(settings, seed.manager),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert resp.json()["reviewer_id"] == str(seed.manager.id)


async def test_a_manager_cannot_approve_outside_their_team(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Being a manager is not the permission — being THEIR manager is. The
    outsider reports to nobody, so this manager has no say over their leave."""
    leave = await _apply_as(client, settings, seed.outsider)

    resp = await client.post(
        f"/api/v1/leaves/{leave['id']}/decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.manager),
    )

    assert resp.status_code in (403, 404)


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


async def test_applying_emails_the_reporting_manager_and_nobody_else(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Applying for leave mails the one person who must act on it.

    Every admin used to be mailed too. They can no longer see or decide a leave
    request, so that was noise - and worse, it pointed them at a page that would
    now 404. The requester is never mailed about their own request.
    """
    await _apply_as(client, settings, seed.report)

    mailed = {to for kind, to in _FakeEmailService.outbox if kind == "leave_request"}
    assert seed.manager.work_email in mailed
    assert seed.admin.work_email not in mailed
    assert seed.report.work_email not in mailed


async def test_an_admin_applying_does_not_email_themselves(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _apply_as(client, settings, seed.admin)

    mailed = {to for kind, to in _FakeEmailService.outbox if kind == "leave_request"}
    assert seed.admin.work_email not in mailed


async def test_an_admin_does_not_see_or_decide_other_peoples_leave(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Org decision: why someone took time off is theirs and their reporting
    manager's business. Running the workspace is not a reason to read it.

    An admin who manages people still sees their OWN reports - that is the next
    test - but not the org.
    """
    leave = await _apply_as(client, settings, seed.report)  # reports to seed.manager

    listed = await client.get("/api/v1/leaves", headers=auth_headers(settings, seed.admin))
    assert leave["id"] not in [r["id"] for r in listed.json()["items"]]

    resp = await client.post(
        f"/api/v1/leaves/{leave['id']}/decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code in (403, 404)


async def test_an_admin_still_handles_their_own_reports_leave(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Narrowing admin must not strand their own team: an admin who is somebody's
    reporting manager acts as that manager, like any other lead."""
    from app.models.employee import Employee, EmployeeStatus, Role

    mine = Employee(
        hr_external_id="hr-admin-report",
        work_email="admin.report@corp.test",
        full_name="Ana Report",
        role=Role.EMPLOYEE,
        manager_id=seed.admin.id,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(mine)
    await db.commit()

    leave = await _apply_as(client, settings, mine)
    listed = await client.get("/api/v1/leaves", headers=auth_headers(settings, seed.admin))
    assert leave["id"] in [r["id"] for r in listed.json()["items"]]

    resp = await client.post(
        f"/api/v1/leaves/{leave['id']}/decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


async def test_hr_still_sees_and_decides_so_a_managerless_request_is_not_stranded(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """HR keeps the org-wide view: it administers balances and quotas, and it is
    who a request from someone with NO reporting manager falls to. Without this
    such a request could never be actioned by anyone at all."""
    from app.models.employee import Employee, EmployeeStatus, Role

    hr = Employee(
        hr_external_id="hr-person",
        work_email="hr.person@corp.test",
        full_name="Hana HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()

    # seed.outsider reports to nobody.
    leave = await _apply_as(client, settings, seed.outsider)
    listed = await client.get("/api/v1/leaves", headers=auth_headers(settings, hr))
    assert leave["id"] in [r["id"] for r in listed.json()["items"]]

    resp = await client.post(
        f"/api/v1/leaves/{leave['id']}/decision",
        json={"approve": True},
        headers=auth_headers(settings, hr),
    )
    assert resp.status_code == 200, resp.text

"""Regularization — self request, manager review, monthly credit cap, scoping."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.notification import Notification, NotificationKind
from tests.conftest import _Seed, auth_headers


async def _request(client: AsyncClient, settings: Settings, actor, day: str):
    return await client.post(
        "/api/v1/attendance/regularizations",
        json={"day": day, "reason": "stuck in traffic"},
        headers=auth_headers(settings, actor),
    )


async def test_request_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.post("/api/v1/attendance/regularizations", json={"day": "2026-06-01"})
    assert resp.status_code == 401


async def test_request_and_manager_approves(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    req = await _request(client, settings, seed.report, "2026-06-02")
    assert req.status_code == 201, req.text
    rid = req.json()["id"]
    assert req.json()["status"] == "pending"

    # Manager sees it in scope and approves.
    listed = await client.get(
        "/api/v1/attendance/regularizations", headers=auth_headers(settings, seed.manager)
    )
    assert rid in [r["id"] for r in listed.json()]
    ok = await client.post(
        f"/api/v1/attendance/regularizations/{rid}/review",
        json={"approve": True},
        headers=auth_headers(settings, seed.manager),
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved"


async def test_duplicate_day_rejected(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    assert (await _request(client, settings, seed.report, "2026-06-03")).status_code == 201
    assert (await _request(client, settings, seed.report, "2026-06-03")).status_code == 422


async def test_non_manager_cannot_review(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    rid = (await _request(client, settings, seed.report, "2026-06-04")).json()["id"]
    resp = await client.post(
        f"/api/v1/attendance/regularizations/{rid}/review",
        json={"approve": True},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 403


async def test_a_team_lead_without_the_manager_role_can_still_review_their_report(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Being a manager is not the permission - being THEIR manager is.

    The gate was `caller.is_manager`, a ROLE check, so a team lead carrying the
    EMPLOYEE or EXECUTIVE role could see their reports' requests and got a 403 on
    every attempt to action one. Five of one lead's sat pending with nobody able
    to clear them.
    """
    from app.models.employee import Employee, EmployeeStatus, Role

    lead = Employee(
        hr_external_id="hr-lead",
        work_email="lead@corp.test",
        full_name="Lee Lead",
        role=Role.EMPLOYEE,  # runs a team without the title
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(lead)
    await db.flush()
    junior = Employee(
        hr_external_id="hr-junior",
        work_email="junior@corp.test",
        full_name="Jun Junior",
        role=Role.EMPLOYEE,
        manager_id=lead.id,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(junior)
    await db.commit()

    rid = (await _request(client, settings, junior, "2026-06-05")).json()["id"]
    resp = await client.post(
        f"/api/v1/attendance/regularizations/{rid}/review",
        json={"approve": True},
        headers=auth_headers(settings, lead),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


async def test_a_team_lead_cannot_review_outside_their_own_team(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The relationship is the permission, so it must not become a free pass:
    a lead has no say over someone who does not report to them."""
    from app.models.employee import Employee, EmployeeStatus, Role

    lead = Employee(
        hr_external_id="hr-lead2",
        work_email="lead2@corp.test",
        full_name="Lou Lead",
        role=Role.EMPLOYEE,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(lead)
    await db.flush()
    junior = Employee(
        hr_external_id="hr-junior2",
        work_email="junior2@corp.test",
        full_name="Jay Junior",
        role=Role.EMPLOYEE,
        manager_id=lead.id,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(junior)
    await db.commit()

    # seed.report belongs to seed.manager, not to this lead.
    rid = (await _request(client, settings, seed.report, "2026-06-06")).json()["id"]
    resp = await client.post(
        f"/api/v1/attendance/regularizations/{rid}/review",
        json={"approve": True},
        headers=auth_headers(settings, lead),
    )
    assert resp.status_code in (403, 404)


async def test_monthly_credit_cap(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    # Default policy allows 2 approvals/month; the 3rd is rejected.
    for d in ("2026-07-01", "2026-07-02", "2026-07-03"):
        await _request(client, settings, seed.report, d)
    listed = (
        await client.get(
            "/api/v1/attendance/regularizations?month=2026-07",
            headers=auth_headers(settings, seed.manager),
        )
    ).json()
    ids = [r["id"] for r in listed]
    results = []
    for rid in ids:
        r = await client.post(
            f"/api/v1/attendance/regularizations/{rid}/review",
            json={"approve": True},
            headers=auth_headers(settings, seed.manager),
        )
        results.append(r.status_code)
    assert results.count(200) == 2
    assert 422 in results  # the 3rd exceeds the monthly credit cap


async def test_requesting_notifies_the_reporting_manager(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """A request used to be written with nobody told. The manager could see and
    approve it all along — but only if they happened to open the page, which is
    how a pile of them sat pending for weeks.
    """
    resp = await _request(client, settings, seed.report, "2026-06-09")
    assert resp.status_code == 201, resp.text

    rows = (
        (await db.execute(select(Notification).where(Notification.recipient_id == seed.manager.id)))
        .scalars()
        .all()
    )
    kinds = [n.kind for n in rows]
    assert NotificationKind.REGULARIZATION_REQUEST in kinds
    note = next(n for n in rows if n.kind is NotificationKind.REGULARIZATION_REQUEST)
    assert "2026-06-09" in (note.body or "")
    assert note.actor_id == seed.report.id


async def test_a_request_from_someone_with_no_manager_notifies_nobody(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The outsider reports to nobody — that must be a quiet no-op, not a crash."""
    resp = await _request(client, settings, seed.outsider, "2026-06-10")

    assert resp.status_code == 201
    rows = (
        (
            await db.execute(
                select(Notification).where(
                    Notification.kind == NotificationKind.REGULARIZATION_REQUEST
                )
            )
        )
        .scalars()
        .all()
    )
    assert all(n.actor_id != seed.outsider.id for n in rows)


async def test_a_viewer_never_picks_up_reports_from_the_org_chart(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """VIEWER is read-only within an EXPLICITLY granted scope, so it must not
    inherit people just because `manager_id` happens to point at it - otherwise a
    contractor left on someone's reporting line gains their screenshots."""
    from app.models.employee import Employee, EmployeeStatus, Role

    viewer = Employee(
        hr_external_id="hr-viewer",
        work_email="viewer@corp.test",
        full_name="Vic Viewer",
        role=Role.VIEWER,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(viewer)
    await db.flush()
    intern = Employee(
        hr_external_id="hr-intern",
        work_email="intern@corp.test",
        full_name="Ira Intern",
        role=Role.EMPLOYEE,
        manager_id=viewer.id,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(intern)
    await db.commit()

    rid = (await _request(client, settings, intern, "2026-06-07")).json()["id"]
    listed = await client.get(
        "/api/v1/attendance/regularizations", headers=auth_headers(settings, viewer)
    )
    assert rid not in [r["id"] for r in listed.json()]
    resp = await client.post(
        f"/api/v1/attendance/regularizations/{rid}/review",
        json={"approve": True},
        headers=auth_headers(settings, viewer),
    )
    assert resp.status_code in (403, 404)

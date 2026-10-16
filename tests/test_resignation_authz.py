"""Resignation authorization — employee submits/withdraws; HR/Admin decide."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.notification import Notification, NotificationKind
from tests.conftest import _Seed, auth_headers


def _body() -> dict[str, object]:
    last_day = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    return {"reason": "Moving on", "last_working_day": last_day}


async def _submit(client: AsyncClient, settings: Settings, actor: object) -> dict[str, object]:
    resp = await client.post(
        "/api/v1/resignations", json=_body(), headers=auth_headers(settings, actor)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_unauthenticated_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/resignations")).status_code == 401


async def test_last_working_day_cannot_be_past(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    past = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    resp = await client.post(
        "/api/v1/resignations",
        json={"last_working_day": past},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422


async def test_employee_submits_and_sees_only_own(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    mine = await _submit(client, settings, seed.report)
    await _submit(client, settings, seed.outsider)  # a different person's resignation

    listed = await client.get("/api/v1/resignations", headers=auth_headers(settings, seed.report))
    ids = [r["id"] for r in listed.json()["items"]]
    assert ids == [mine["id"]]  # the report sees only their own


async def test_duplicate_active_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _submit(client, settings, seed.report)
    dup = await client.post(
        "/api/v1/resignations", json=_body(), headers=auth_headers(settings, seed.report)
    )
    assert dup.status_code == 409


async def test_only_hr_admin_decides_never_own(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await _submit(client, settings, seed.report)
    rid = res["id"]

    # The employee can't decide their own resignation.
    self_decide = await client.post(
        f"/api/v1/resignations/{rid}/decision",
        json={"accept": True},
        headers=auth_headers(settings, seed.report),
    )
    assert self_decide.status_code == 403

    # A manager (not HR/Admin) can't decide either.
    mgr = await client.post(
        f"/api/v1/resignations/{rid}/decision",
        json={"accept": True},
        headers=auth_headers(settings, seed.manager),
    )
    assert mgr.status_code == 403

    # Admin accepts it.
    ok = await client.post(
        f"/api/v1/resignations/{rid}/decision",
        json={"accept": True, "note": "All the best"},
        headers=auth_headers(settings, seed.admin),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "accepted"

    # A decided resignation can't be decided again.
    again = await client.post(
        f"/api/v1/resignations/{rid}/decision",
        json={"accept": False},
        headers=auth_headers(settings, seed.admin),
    )
    assert again.status_code == 409


async def test_employee_withdraws_own(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    res = await _submit(client, settings, seed.report)
    rid = res["id"]
    # An outsider can't withdraw someone else's resignation - and, being out of
    # scope, is not even told it exists (404, not 403).
    other = await client.post(
        f"/api/v1/resignations/{rid}/withdraw", headers=auth_headers(settings, seed.outsider)
    )
    assert other.status_code == 404
    # Their manager can see it but it is not theirs to withdraw.
    mgr = await client.post(
        f"/api/v1/resignations/{rid}/withdraw", headers=auth_headers(settings, seed.manager)
    )
    assert mgr.status_code == 403
    mine = await client.post(
        f"/api/v1/resignations/{rid}/withdraw", headers=auth_headers(settings, seed.report)
    )
    assert mine.status_code == 200
    assert mine.json()["status"] == "withdrawn"


async def test_the_reporting_manager_sees_but_cannot_decide(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A manager has to plan around a report leaving, so they see it - read-only.
    Deciding stays with HR/Admin."""
    res = await _submit(client, settings, seed.report)
    await _submit(client, settings, seed.outsider)  # not their report
    mgr = auth_headers(settings, seed.manager)

    listed = await client.get("/api/v1/resignations", headers=mgr)
    assert [r["id"] for r in listed.json()["items"]] == [res["id"]]
    one = await client.get(f"/api/v1/resignations/{res['id']}", headers=mgr)
    assert one.status_code == 200
    decide = await client.post(
        f"/api/v1/resignations/{res['id']}/decision", json={"accept": True}, headers=mgr
    )
    assert decide.status_code == 403


async def test_the_manager_does_not_see_the_reason_or_the_decision_note(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The manager learns THAT a report resigned and their last day - not why,
    and not what HR wrote back. The resigner and HR/Admin still see both."""
    res = await _submit(client, settings, seed.report)
    decided = await client.post(
        f"/api/v1/resignations/{res['id']}/decision",
        json={"accept": True, "note": "Exit interview on Friday"},
        headers=auth_headers(settings, seed.admin),
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["reason"] == "Moving on"
    assert decided.json()["decision_note"] == "Exit interview on Friday"

    mgr = auth_headers(settings, seed.manager)
    one = (await client.get(f"/api/v1/resignations/{res['id']}", headers=mgr)).json()
    listed = (await client.get("/api/v1/resignations", headers=mgr)).json()["items"]
    for row in (one, listed[0]):
        assert row["reason"] is None
        assert row["decision_note"] is None
        assert row["last_working_day"] == res["last_working_day"]
        assert row["status"] == "accepted"

    own = await client.get(
        f"/api/v1/resignations/{res['id']}", headers=auth_headers(settings, seed.report)
    )
    assert own.json()["reason"] == "Moving on"
    assert own.json()["decision_note"] == "Exit interview on Friday"


async def test_out_of_scope_callers_get_404(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await _submit(client, settings, seed.report)
    other = auth_headers(settings, seed.outsider)
    assert (await client.get(f"/api/v1/resignations/{res['id']}", headers=other)).status_code == 404
    decide = await client.post(
        f"/api/v1/resignations/{res['id']}/decision", json={"accept": True}, headers=other
    )
    assert decide.status_code == 404


async def test_submitting_notifies_hr_admins_including_it_admins_and_the_manager(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = Employee(
        hr_external_id="hr-person",
        work_email="hr.person@corp.test",
        full_name="Hana HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    it_admin = Employee(
        hr_external_id="hr-it-admin",
        work_email="it.admin@corp.test",
        full_name="Ivo IT",
        role=Role.IT_ADMIN,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add_all([hr, it_admin])
    await db.commit()

    await _submit(client, settings, seed.report)

    rows = await db.execute(
        select(Notification.recipient_id, Notification.title).where(
            Notification.kind == NotificationKind.RESIGNATION_SUBMITTED
        )
    )
    told = dict(rows.tuples().all())
    assert {hr.id, it_admin.id, seed.admin.id, seed.manager.id} <= set(told)
    assert told[seed.manager.id] == "Resignation from your team"
    assert seed.report.id not in told

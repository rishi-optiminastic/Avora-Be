"""Reimbursement authorization — employee submits; manager then HR/Admin decide.

Proves the two-step segregation of duties and the repository row-scope
(be/CLAUDE.md §9): out-of-scope users get 403/404, and no step can be skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


def _body(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "amount_minor": 250_00,
        "category": "travel",
        "description": "Client visit cab fare",
        "expense_date": datetime.now(UTC).date().isoformat(),
    }
    body.update(over)
    return body


async def _submit(client: AsyncClient, settings: Settings, actor: object) -> dict[str, object]:
    resp = await client.post(
        "/api/v1/reimbursements", json=_body(), headers=auth_headers(settings, actor)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_unauthenticated_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/reimbursements")).status_code == 401


async def test_expense_date_cannot_be_future(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    future = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    resp = await client.post(
        "/api/v1/reimbursements",
        json=_body(expense_date=future),
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422


async def test_employee_sees_own_manager_sees_report_outsider_does_not(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    mine = await _submit(client, settings, seed.report)

    own = await client.get("/api/v1/reimbursements", headers=auth_headers(settings, seed.report))
    assert [r["id"] for r in own.json()["items"]] == [mine["id"]]

    mgr = await client.get("/api/v1/reimbursements", headers=auth_headers(settings, seed.manager))
    assert mine["id"] in [r["id"] for r in mgr.json()["items"]]  # manager sees the report's claim

    out = await client.get(
        "/api/v1/reimbursements", headers=auth_headers(settings, seed.outsider)
    )
    assert out.json()["total"] == 0  # unrelated employee sees nothing

    # IDOR: outsider can't read the claim by id either.
    direct = await client.get(
        f"/api/v1/reimbursements/{mine['id']}", headers=auth_headers(settings, seed.outsider)
    )
    assert direct.status_code == 404


async def test_two_step_happy_path(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    rid = (await _submit(client, settings, seed.report))["id"]

    # Step 1: the reporting manager approves.
    step1 = await client.post(
        f"/api/v1/reimbursements/{rid}/manager-decision",
        json={"approve": True, "note": "Legit"},
        headers=auth_headers(settings, seed.manager),
    )
    assert step1.status_code == 200, step1.text
    assert step1.json()["status"] == "manager_approved"

    # Step 2: HR/Admin gives final approval.
    step2 = await client.post(
        f"/api/v1/reimbursements/{rid}/hr-decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.admin),
    )
    assert step2.status_code == 200, step2.text
    assert step2.json()["status"] == "approved"


async def test_steps_cannot_be_skipped_or_done_by_wrong_actor(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    rid = (await _submit(client, settings, seed.report))["id"]

    # Applicant can't approve their own claim at the manager step.
    assert (
        await client.post(
            f"/api/v1/reimbursements/{rid}/manager-decision",
            json={"approve": True},
            headers=auth_headers(settings, seed.report),
        )
    ).status_code == 403

    # An unrelated employee (not the manager) can't do the manager step.
    assert (
        await client.post(
            f"/api/v1/reimbursements/{rid}/manager-decision",
            json={"approve": True},
            headers=auth_headers(settings, seed.outsider),
        )
    ).status_code == 403

    # HR step can't run before the manager step (still 'submitted').
    assert (
        await client.post(
            f"/api/v1/reimbursements/{rid}/hr-decision",
            json={"approve": True},
            headers=auth_headers(settings, seed.admin),
        )
    ).status_code == 409

    # Manager approves → now a plain manager can't do the HR step (not HR/Admin).
    ok = await client.post(
        f"/api/v1/reimbursements/{rid}/manager-decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.manager),
    )
    assert ok.status_code == 200
    assert (
        await client.post(
            f"/api/v1/reimbursements/{rid}/hr-decision",
            json={"approve": True},
            headers=auth_headers(settings, seed.manager),
        )
    ).status_code == 403


async def test_manager_rejection_ends_the_claim(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    rid = (await _submit(client, settings, seed.report))["id"]
    rej = await client.post(
        f"/api/v1/reimbursements/{rid}/manager-decision",
        json={"approve": False, "note": "No receipt"},
        headers=auth_headers(settings, seed.manager),
    )
    assert rej.status_code == 200
    assert rej.json()["status"] == "rejected"
    # A rejected claim can't then be pushed through HR.
    assert (
        await client.post(
            f"/api/v1/reimbursements/{rid}/hr-decision",
            json={"approve": True},
            headers=auth_headers(settings, seed.admin),
        )
    ).status_code == 409


async def test_withdraw_is_owner_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    rid = (await _submit(client, settings, seed.report))["id"]
    assert (
        await client.post(
            f"/api/v1/reimbursements/{rid}/withdraw",
            headers=auth_headers(settings, seed.outsider),
        )
    ).status_code == 403
    mine = await client.post(
        f"/api/v1/reimbursements/{rid}/withdraw", headers=auth_headers(settings, seed.report)
    )
    assert mine.status_code == 200
    assert mine.json()["status"] == "withdrawn"

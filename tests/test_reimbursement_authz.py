"""Reimbursement authorization — employee submits; manager then HR/Admin decide.

Proves the two-step segregation of duties and the repository row-scope
(be/CLAUDE.md §9): out-of-scope users get 403/404, and no step can be skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers


async def _hr(db: AsyncSession) -> Employee:
    """An HR reviewer, created per test rather than added to the shared seed.

    Reimbursement review is HR's, not Admin's, so these tests need an HR actor —
    but putting one in the shared fixture shifts head-counts in unrelated
    org-wide assertions, so it is local, matching test_payroll.
    """
    existing = await db.scalar(select(Employee).where(Employee.work_email == "reviewer@corp.test"))
    if existing is not None:
        return existing
    person = Employee(
        hr_external_id="hr-reviewer",
        work_email="reviewer@corp.test",
        full_name="Hana HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(person)
    await db.commit()
    return person


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

    out = await client.get("/api/v1/reimbursements", headers=auth_headers(settings, seed.outsider))
    assert out.json()["total"] == 0  # unrelated employee sees nothing

    # IDOR: outsider can't read the claim by id either.
    direct = await client.get(
        f"/api/v1/reimbursements/{mine['id']}", headers=auth_headers(settings, seed.outsider)
    )
    assert direct.status_code == 404


async def test_two_step_happy_path(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
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
        headers=auth_headers(settings, await _hr(db)),
    )
    assert step2.status_code == 200, step2.text
    assert step2.json()["status"] == "approved"


async def test_steps_cannot_be_skipped_or_done_by_wrong_actor(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
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
            headers=auth_headers(settings, await _hr(db)),
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
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
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
            headers=auth_headers(settings, await _hr(db)),
        )
    ).status_code == 409


async def test_withdraw_is_owner_only(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
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


async def test_admin_cannot_see_or_decide_someone_elses_claim(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A claim is personal spending data. Running the workspace is not a reason to
    read it — reimbursements belong to HR and finance, not Admin.

    Note this covers `it_admin` too: it normalises to ADMIN at CurrentUser, so the
    two cannot be told apart by any rule downstream.
    """
    claim_id = (await _submit(client, settings, seed.report))["id"]

    listed = await client.get("/api/v1/reimbursements", headers=auth_headers(settings, seed.admin))
    assert listed.status_code == 200
    assert all(r["employee_id"] != str(seed.report.id) for r in listed.json()["items"])

    # 404 not 403 — revealing existence would leak scope (§7).
    fetched = await client.get(
        f"/api/v1/reimbursements/{claim_id}", headers=auth_headers(settings, seed.admin)
    )
    assert fetched.status_code == 404


async def test_a_payroll_manager_reviews_reimbursements(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The finance person who actually pays claims out gets HR's powers here via
    the payroll-manager grant, without being made HR."""
    seed.outsider.payroll_manager = True
    await db.commit()

    claim_id = (await _submit(client, settings, seed.report))["id"]
    approved = await client.post(
        f"/api/v1/reimbursements/{claim_id}/manager-decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.manager),
    )
    assert approved.status_code == 200, approved.text

    decided = await client.post(
        f"/api/v1/reimbursements/{claim_id}/hr-decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.outsider),
    )
    assert decided.status_code == 200, decided.text


async def test_an_admin_still_sees_their_own_claim(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Excluding Admin from others' claims must not stop them filing their own."""
    claim_id = (await _submit(client, settings, seed.admin))["id"]
    fetched = await client.get(
        f"/api/v1/reimbursements/{claim_id}", headers=auth_headers(settings, seed.admin)
    )
    assert fetched.status_code == 200


_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


async def test_claimant_attaches_an_invoice_and_a_reviewer_can_read_it(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    claim_id = (await _submit(client, settings, seed.report))["id"]

    attached = await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipt",
        files={"file": ("invoice.png", _PNG, "image/png")},
        headers=auth_headers(settings, seed.report),
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["has_receipt"] is True

    # A reviewer must be able to see what they're approving.
    got = await client.get(
        f"/api/v1/reimbursements/{claim_id}/receipt",
        headers=auth_headers(settings, await _hr(db)),
    )
    assert got.status_code == 200
    assert got.content == _PNG


async def test_someone_else_cannot_attach_to_your_claim(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    claim_id = (await _submit(client, settings, seed.report))["id"]
    resp = await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipt",
        files={"file": ("invoice.png", _PNG, "image/png")},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_an_outsider_cannot_read_the_invoice(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    claim_id = (await _submit(client, settings, seed.report))["id"]
    await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipt",
        files={"file": ("invoice.png", _PNG, "image/png")},
        headers=auth_headers(settings, seed.report),
    )
    # 404, never 403 — revealing existence would leak scope (§7).
    resp = await client.get(
        f"/api/v1/reimbursements/{claim_id}/receipt",
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 404


async def test_an_executable_upload_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """An invoice is a PDF or a photo. Anything else never reaches storage."""
    claim_id = (await _submit(client, settings, seed.report))["id"]
    resp = await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipt",
        files={"file": ("payload.html", b"<script>alert(1)</script>", "text/html")},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422


async def test_the_invoice_is_frozen_once_the_claim_is_approved(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """After final approval the attachment is part of a paid record."""
    claim_id = (await _submit(client, settings, seed.report))["id"]
    await client.post(
        f"/api/v1/reimbursements/{claim_id}/manager-decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.manager),
    )
    await client.post(
        f"/api/v1/reimbursements/{claim_id}/hr-decision",
        json={"approve": True},
        headers=auth_headers(settings, await _hr(db)),
    )

    resp = await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipt",
        files={"file": ("swapped.png", _PNG, "image/png")},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422

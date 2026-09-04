"""Reimbursement authorization — employee submits; manager then HR/Admin decide.

Proves the two-step segregation of duties and the repository row-scope
(be/CLAUDE.md §9): out-of-scope users get 403/404, and no step can be skipped.
"""

from __future__ import annotations

import uuid
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
        f"/api/v1/reimbursements/{claim_id}/receipts",
        files={"file": ("invoice.png", _PNG, "image/png")},
        headers=auth_headers(settings, seed.report),
    )
    assert attached.status_code == 201, attached.text
    body = attached.json()
    assert body["has_receipt"] is True
    assert [r["label"] for r in body["receipts"]] == ["invoice"]  # named from the file

    # A reviewer must be able to see what they're approving.
    got = await client.get(
        f"/api/v1/reimbursements/{claim_id}/receipts/{body['receipts'][0]['id']}",
        headers=auth_headers(settings, await _hr(db)),
    )
    assert got.status_code == 200
    assert got.content == _PNG


async def test_someone_else_cannot_attach_to_your_claim(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    claim_id = (await _submit(client, settings, seed.report))["id"]
    resp = await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipts",
        files={"file": ("invoice.png", _PNG, "image/png")},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_an_outsider_cannot_read_the_invoice(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    claim_id = (await _submit(client, settings, seed.report))["id"]
    await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipts",
        files={"file": ("invoice.png", _PNG, "image/png")},
        headers=auth_headers(settings, seed.report),
    )
    # 404, never 403 — revealing existence would leak scope (§7).
    resp = await client.get(
        f"/api/v1/reimbursements/{claim_id}/receipts/{uuid.uuid4()}",
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 404


async def test_an_executable_upload_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """An invoice is a PDF or a photo. Anything else never reaches storage."""
    claim_id = (await _submit(client, settings, seed.report))["id"]
    resp = await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipts",
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
        f"/api/v1/reimbursements/{claim_id}/receipts",
        files={"file": ("swapped.png", _PNG, "image/png")},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422


async def test_several_named_proofs_can_be_attached_and_removed(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A claim usually needs more than one document, and a reviewer has to be able
    to tell them apart without opening every file."""
    claim_id = (await _submit(client, settings, seed.report))["id"]
    headers = auth_headers(settings, seed.report)

    for label, name in (("Cab invoice", "cab.png"), ("Toll receipt", "toll.png")):
        added = await client.post(
            f"/api/v1/reimbursements/{claim_id}/receipts",
            files={"file": (name, _PNG, "image/png")},
            data={"label": label},
            headers=headers,
        )
        assert added.status_code == 201, added.text

    body = added.json()
    assert [r["label"] for r in body["receipts"]] == ["Cab invoice", "Toll receipt"]
    assert body["has_receipt"] is True
    assert all(r["size_bytes"] == len(_PNG) for r in body["receipts"])
    # The bytes and the storage key are never handed back inline.
    assert "content" not in body["receipts"][0]
    assert "object_key" not in body["receipts"][0]

    # A reviewer can open each one individually.
    hr = await _hr(db)
    for r in body["receipts"]:
        got = await client.get(
            f"/api/v1/reimbursements/{claim_id}/receipts/{r['id']}",
            headers=auth_headers(settings, hr),
        )
        assert got.status_code == 200
        assert got.content == _PNG

    # The claimant can drop one; the other survives.
    dropped = await client.delete(
        f"/api/v1/reimbursements/{claim_id}/receipts/{body['receipts'][0]['id']}",
        headers=headers,
    )
    assert dropped.status_code == 200, dropped.text
    assert [r["label"] for r in dropped.json()["receipts"]] == ["Toll receipt"]


async def test_a_proof_id_from_another_claim_does_not_resolve(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The claim is what gets scope-checked, so the proof must be proven to belong
    to it — otherwise a valid id from someone else's claim would read through."""
    mine = (await _submit(client, settings, seed.report))["id"]
    theirs = (await _submit(client, settings, seed.manager))["id"]
    attached = await client.post(
        f"/api/v1/reimbursements/{theirs}/receipts",
        files={"file": ("theirs.png", _PNG, "image/png")},
        headers=auth_headers(settings, seed.manager),
    )
    assert attached.status_code == 201
    foreign_id = attached.json()["receipts"][0]["id"]

    resp = await client.get(
        f"/api/v1/reimbursements/{mine}/receipts/{foreign_id}",
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 404


async def test_someone_else_cannot_remove_your_proof(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    claim_id = (await _submit(client, settings, seed.report))["id"]
    added = await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipts",
        files={"file": ("invoice.png", _PNG, "image/png")},
        headers=auth_headers(settings, seed.report),
    )
    receipt_id = added.json()["receipts"][0]["id"]

    # The manager may READ it to review, but never delete it.
    resp = await client.delete(
        f"/api/v1/reimbursements/{claim_id}/receipts/{receipt_id}",
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_an_unnamed_proof_still_gets_a_label(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A nameless row in a reviewer's list is worse than a generic one."""
    claim_id = (await _submit(client, settings, seed.report))["id"]
    # An extension with no stem: nothing to derive a name from either way.
    added = await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipts",
        files={"file": (".png", _PNG, "image/png")},
        headers=auth_headers(settings, seed.report),
    )
    assert added.status_code == 201, added.text
    assert added.json()["receipts"][0]["label"] == "Proof"


async def test_a_claim_cannot_carry_unlimited_proofs(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    claim_id = (await _submit(client, settings, seed.report))["id"]
    headers = auth_headers(settings, seed.report)
    for i in range(10):
        ok = await client.post(
            f"/api/v1/reimbursements/{claim_id}/receipts",
            files={"file": (f"p{i}.png", _PNG, "image/png")},
            headers=headers,
        )
        assert ok.status_code == 201, ok.text

    too_many = await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipts",
        files={"file": ("eleventh.png", _PNG, "image/png")},
        headers=headers,
    )
    assert too_many.status_code == 422


_PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"


async def test_a_pdf_is_accepted_even_when_the_browser_calls_it_something_else(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Plenty of systems hand a PDF over as application/octet-stream (or with no
    type at all). Rejecting those rejected real receipts, so the bytes decide."""
    claim_id = (await _submit(client, settings, seed.report))["id"]
    headers = auth_headers(settings, seed.report)

    for content_type in ("application/octet-stream", "application/pdf", ""):
        added = await client.post(
            f"/api/v1/reimbursements/{claim_id}/receipts",
            files={"file": ("bill.pdf", _PDF, content_type)},
            headers=headers,
        )
        assert added.status_code == 201, f"{content_type!r}: {added.text}"

    assert len(added.json()["receipts"]) == 3


async def test_a_script_calling_itself_a_pdf_is_still_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The other half of not trusting the header: the claim cuts both ways."""
    claim_id = (await _submit(client, settings, seed.report))["id"]
    resp = await client.post(
        f"/api/v1/reimbursements/{claim_id}/receipts",
        files={"file": ("invoice.pdf", b"<script>alert(1)</script>", "application/pdf")},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422


# --- which payroll month settles the claim ----------------------------------- #
async def _to_hr_step(client: AsyncClient, settings: Settings, seed: _Seed) -> str:
    claim_id = (await _submit(client, settings, seed.report))["id"]
    approved = await client.post(
        f"/api/v1/reimbursements/{claim_id}/manager-decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.manager),
    )
    assert approved.status_code == 200, approved.text
    return str(claim_id)


async def test_hr_chooses_which_payroll_month_settles_the_claim(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """An August expense claimed late in September has already missed the payrun
    that settled August, so HR must be able to push it to the next open month."""
    claim_id = await _to_hr_step(client, settings, seed)
    filed_against = (
        await client.get(
            f"/api/v1/reimbursements/{claim_id}", headers=auth_headers(settings, seed.report)
        )
    ).json()["period_month"]

    decided = await client.post(
        f"/api/v1/reimbursements/{claim_id}/hr-decision",
        json={"approve": True, "settlement_month": "2099-11"},
        headers=auth_headers(settings, await _hr(db)),
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["period_month"] == "2099-11"
    assert decided.json()["period_month"] != filed_against


async def test_omitting_the_month_keeps_the_claim_where_it_was_filed(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The expense month stays the default — HR only overrides when they need to."""
    claim_id = await _to_hr_step(client, settings, seed)
    before = (
        await client.get(
            f"/api/v1/reimbursements/{claim_id}", headers=auth_headers(settings, seed.report)
        )
    ).json()["period_month"]

    decided = await client.post(
        f"/api/v1/reimbursements/{claim_id}/hr-decision",
        json={"approve": True},
        headers=auth_headers(settings, await _hr(db)),
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["period_month"] == before


async def test_a_claim_cannot_be_settled_into_an_already_released_month(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Released payslips are frozen snapshots, so money added to that month would
    never actually be paid. Refuse loudly instead of approving into a void."""
    from app.models.payslip import Payslip

    db.add(Payslip(employee_id=seed.report.id, period_month="2099-11", net_minor=1))
    await db.commit()

    claim_id = await _to_hr_step(client, settings, seed)
    resp = await client.post(
        f"/api/v1/reimbursements/{claim_id}/hr-decision",
        json={"approve": True, "settlement_month": "2099-11"},
        headers=auth_headers(settings, await _hr(db)),
    )
    assert resp.status_code == 409, resp.text
    assert "already been released" in resp.text


async def test_a_rejection_never_moves_the_month(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    claim_id = await _to_hr_step(client, settings, seed)
    before = (
        await client.get(
            f"/api/v1/reimbursements/{claim_id}", headers=auth_headers(settings, seed.report)
        )
    ).json()["period_month"]

    resp = await client.post(
        f"/api/v1/reimbursements/{claim_id}/hr-decision",
        json={"approve": False, "settlement_month": "2099-12"},
        headers=auth_headers(settings, await _hr(db)),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["period_month"] == before


async def test_a_malformed_settlement_month_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    claim_id = await _to_hr_step(client, settings, seed)
    for bad in ("2099-13", "99-01", "November", "2099-1"):
        resp = await client.post(
            f"/api/v1/reimbursements/{claim_id}/hr-decision",
            json={"approve": True, "settlement_month": bad},
            headers=auth_headers(settings, await _hr(db)),
        )
        assert resp.status_code == 422, f"{bad}: {resp.text}"

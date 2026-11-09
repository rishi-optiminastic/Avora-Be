"""Access - tech assets, who holds them, the reveal, and requests.

Authorization is most of this (CLAUDE.md section 9): who sees holders, who sees
cost, who may reveal a password, who may approve. The other half is that a
secret never appears in any response except the audited reveal.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.audit import AuditLog
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

BASE = "/api/v1/access"
_SECRET = "hunter2-correct-horse"


async def _hr(db: AsyncSession) -> Employee:
    hr = Employee(
        hr_external_id="hr-hr",
        work_email="hr@corp.test",
        full_name="Holly HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    return hr


async def _asset(client: AsyncClient, settings: Settings, seed: _Seed, **over: object) -> dict:
    body: dict[str, object] = {
        "kind": "tool",
        "name": "Vercel",
        "category": "Hosting",
        "login_id": "devops@corp.test",
        "cost_minor": 2_000,
        "currency": "usd",
        "billing_cycle": "Monthly",
        "details": {"plan": "Pro"},
        "secret": _SECRET,
    }
    body.update(over)
    resp = await client.post(
        f"{BASE}/assets", json=body, headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def _grant(
    client: AsyncClient, settings: Settings, granter: Employee, asset_id: str, who: Employee
) -> dict:
    resp = await client.post(
        f"{BASE}/grants",
        json={"asset_id": asset_id, "employee_id": str(who.id), "level": "editor"},
        headers=auth_headers(settings, granter),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


# ---- authentication and asset admin --------------------------------------- #


async def test_every_route_requires_authentication(client: AsyncClient, seed: _Seed) -> None:
    for method, path in (
        ("get", "/me"),
        ("get", "/assets"),
        ("post", "/assets"),
        ("get", "/requests"),
        ("post", "/requests"),
    ):
        resp = await getattr(client, method)(f"{BASE}{path}")
        assert resp.status_code == 401, path


async def test_hr_and_admins_add_and_edit_only_admins_archive_or_rotate(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    for who in (seed.report, seed.manager):
        resp = await client.post(
            f"{BASE}/assets",
            json={"kind": "phone", "name": "+91 77108 22162"},
            headers=auth_headers(settings, who),
        )
        assert resp.status_code == 403
    phone = await client.post(
        f"{BASE}/assets",
        json={"kind": "phone", "name": "+91 77108 22162", "details": {"carrier": "Jio"}},
        headers=auth_headers(settings, hr),
    )
    assert phone.status_code == 201, phone.text
    assert phone.json()["details"] == {"carrier": "Jio"}

    asset = await _asset(client, settings, seed)
    hr_h = auth_headers(settings, hr)
    edit = await client.patch(
        f"{BASE}/assets/{asset['id']}", json={"name": "Vercel Pro"}, headers=hr_h
    )
    assert edit.status_code == 200 and edit.json()["name"] == "Vercel Pro"
    archive = await client.patch(
        f"{BASE}/assets/{asset['id']}", json={"archived": True}, headers=hr_h
    )
    assert archive.status_code == 403
    rotate = await client.put(
        f"{BASE}/assets/{asset['id']}/secret", json={"secret": "new"}, headers=hr_h
    )
    assert rotate.status_code == 403
    by_manager = await client.patch(
        f"{BASE}/assets/{asset['id']}",
        json={"name": "X"},
        headers=auth_headers(settings, seed.manager),
    )
    assert by_manager.status_code == 403


async def test_the_secret_never_appears_in_any_read(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    asset = await _asset(client, settings, seed)
    assert asset["has_secret"] is True
    await _grant(client, settings, seed.admin, asset["id"], seed.report)
    admin, report = auth_headers(settings, seed.admin), auth_headers(settings, seed.report)
    reads = [
        (await client.get(f"{BASE}/assets", headers=admin)).text,
        (await client.get(f"{BASE}/assets/{asset['id']}", headers=admin)).text,
        (await client.get(f"{BASE}/me", headers=report)).text,
    ]
    for text in reads:
        assert _SECRET not in text
        assert "secret_ciphertext" not in text


async def test_a_credential_cannot_hide_in_details(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        f"{BASE}/assets",
        json={"kind": "tool", "name": "Razorpay", "details": {"password": "abc"}},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 422


async def test_archived_assets_leave_the_catalog(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    asset = await _asset(client, settings, seed)
    admin = auth_headers(settings, seed.admin)
    archived = await client.patch(
        f"{BASE}/assets/{asset['id']}", json={"archived": True}, headers=admin
    )
    assert archived.status_code == 200 and archived.json()["archived"] is True
    names = [a["name"] for a in (await client.get(f"{BASE}/assets", headers=admin)).json()]
    assert "Vercel" not in names
    hidden = await client.get(
        f"{BASE}/assets/{asset['id']}", headers=auth_headers(settings, seed.report)
    )
    assert hidden.status_code == 404


# ---- visibility ----------------------------------------------------------- #


async def test_holders_and_cost_follow_the_reporting_line(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The report holds Vercel. Their manager sees that and the cost; an
    unrelated executive sees the asset exists but not who holds it or its cost."""
    hr = await _hr(db)
    asset = await _asset(client, settings, seed)
    await _grant(client, settings, hr, asset["id"], seed.report)

    async def view(who: Employee) -> dict:
        resp = await client.get(f"{BASE}/assets/{asset['id']}", headers=auth_headers(settings, who))
        assert resp.status_code == 200
        return resp.json()  # type: ignore[no-any-return]

    outsider = await view(seed.outsider)
    assert outsider["holder_count"] == 1 and outsider["holders"] == []
    assert outsider["cost_visible"] is False and outsider["cost_minor"] is None
    assert outsider["login_id"] is None and outsider["details"] is None

    manager = await view(seed.manager)
    assert [h["employee_id"] for h in manager["holders"]] == [str(seed.report.id)]
    assert manager["cost_minor"] == 2_000 and manager["currency"] == "USD"

    holder = await view(seed.report)
    assert holder["my_level"] == "editor" and holder["login_id"] == "devops@corp.test"

    hr_view = await view(hr)
    assert hr_view["cost_visible"] is True and len(hr_view["holders"]) == 1


# ---- reveal --------------------------------------------------------------- #


async def test_only_a_holder_or_admin_can_reveal(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    asset = await _asset(client, settings, seed)
    await _grant(client, settings, hr, asset["id"], seed.report)
    url = f"{BASE}/assets/{asset['id']}/reveal"

    for outsider in (seed.manager, seed.outsider, hr):
        assert (await client.post(url, headers=auth_headers(settings, outsider))).status_code == 404

    holder = await client.post(url, headers=auth_headers(settings, seed.report))
    assert holder.status_code == 200
    assert holder.json()["secret"] == _SECRET
    admin = await client.post(url, headers=auth_headers(settings, seed.admin))
    assert admin.status_code == 200

    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert actions.count("access.secret.reveal") == 2
    assert all(
        _SECRET not in (row.target or "") for row in (await db.execute(select(AuditLog))).scalars()
    )


async def test_revoking_ends_the_reveal(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    asset = await _asset(client, settings, seed)
    grant = await _grant(client, settings, seed.admin, asset["id"], seed.report)
    url = f"{BASE}/assets/{asset['id']}/reveal"
    report = auth_headers(settings, seed.report)
    assert (await client.post(url, headers=report)).status_code == 200

    gone = await client.delete(
        f"{BASE}/grants/{grant['id']}", headers=auth_headers(settings, seed.admin)
    )
    assert gone.status_code == 204
    assert (await client.post(url, headers=report)).status_code == 404
    assert (await client.get(f"{BASE}/me", headers=report)).json() == []


async def test_only_hr_and_admins_grant_or_revoke(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    asset = await _asset(client, settings, seed)
    denied = await client.post(
        f"{BASE}/grants",
        json={"asset_id": asset["id"], "employee_id": str(seed.report.id)},
        headers=auth_headers(settings, seed.manager),
    )
    assert denied.status_code == 403
    grant = await _grant(client, settings, seed.admin, asset["id"], seed.report)
    revoke = await client.delete(
        f"{BASE}/grants/{grant['id']}", headers=auth_headers(settings, seed.manager)
    )
    assert revoke.status_code == 403


# ---- requests ------------------------------------------------------------- #


async def _ask(
    client: AsyncClient, settings: Settings, who: Employee, asset_id: str, level: str = "viewer"
) -> dict:
    resp = await client.post(
        f"{BASE}/requests",
        json={"asset_id": asset_id, "level": level, "reason": "Deploying the landing page"},
        headers=auth_headers(settings, who),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def test_the_manager_approves_and_access_is_live_at_once(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    asset = await _asset(client, settings, seed)
    ask = await _ask(client, settings, seed.report, asset["id"])

    queue = (
        await client.get(f"{BASE}/requests", headers=auth_headers(settings, seed.manager))
    ).json()
    assert [(r["id"], r["can_decide"]) for r in queue] == [(ask["id"], True)]

    decided = await client.post(
        f"{BASE}/requests/{ask['id']}/review",
        json={"approve": True},
        headers=auth_headers(settings, seed.manager),
    )
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    mine = (await client.get(f"{BASE}/me", headers=auth_headers(settings, seed.report))).json()
    assert [(g["asset"]["name"], g["level"]) for g in mine] == [("Vercel", "viewer")]


async def test_nobody_outside_the_line_decides_and_never_your_own(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    asset = await _asset(client, settings, seed)
    ask = await _ask(client, settings, seed.report, asset["id"])
    url = f"{BASE}/requests/{ask['id']}/review"

    stranger = await client.post(
        url, json={"approve": True}, headers=auth_headers(settings, seed.outsider)
    )
    assert stranger.status_code == 404
    own = await client.post(
        url, json={"approve": True}, headers=auth_headers(settings, seed.report)
    )
    assert own.status_code == 403

    hr = await _hr(db)
    by_hr = await client.post(url, json={"approve": True}, headers=auth_headers(settings, hr))
    assert by_hr.status_code == 200


async def test_rejecting_needs_a_reason(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    asset = await _asset(client, settings, seed)
    ask = await _ask(client, settings, seed.report, asset["id"])
    url = f"{BASE}/requests/{ask['id']}/review"
    manager = auth_headers(settings, seed.manager)
    assert (await client.post(url, json={"approve": False}, headers=manager)).status_code == 422
    done = await client.post(
        url, json={"approve": False, "review_note": "Use the shared preview link"}, headers=manager
    )
    assert done.status_code == 200 and done.json()["status"] == "rejected"
    assert (
        await client.get(f"{BASE}/me", headers=auth_headers(settings, seed.report))
    ).json() == []


async def test_duplicates_are_refused_but_an_upgrade_is_not(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    asset = await _asset(client, settings, seed)
    await _ask(client, settings, seed.report, asset["id"])
    again = await client.post(
        f"{BASE}/requests",
        json={"asset_id": asset["id"], "reason": "again"},
        headers=auth_headers(settings, seed.report),
    )
    assert again.status_code == 422

    other = await _asset(client, settings, seed, name="GitHub", secret=None)
    await _grant(client, settings, seed.admin, other["id"], seed.report)  # editor
    same = await client.post(
        f"{BASE}/requests",
        json={"asset_id": other["id"], "level": "viewer", "reason": "x"},
        headers=auth_headers(settings, seed.report),
    )
    assert same.status_code == 422
    await _ask(client, settings, seed.report, other["id"], level="admin")


async def test_only_the_requester_cancels(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    asset = await _asset(client, settings, seed)
    ask = await _ask(client, settings, seed.report, asset["id"])
    url = f"{BASE}/requests/{ask['id']}/cancel"
    assert (await client.post(url, headers=auth_headers(settings, seed.manager))).status_code == 404
    done = await client.post(url, headers=auth_headers(settings, seed.report))
    assert done.status_code == 200 and done.json()["status"] == "cancelled"

"""Invitation flow — admin/HR creation and accept-provisions-employee."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers, bearer_for_email


async def test_non_admin_cannot_invite(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # A manager is not admin/HR — inviting is a people-ops function.
    resp = await client.post(
        "/api/v1/invitations",
        json={"email": "newhire@acme.com", "role": "manager"},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_hr_can_invite(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = Employee(
        hr_external_id="hr-inviter",
        work_email="hr-invite@corp.test",
        full_name="Holly HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()

    resp = await client.post(
        "/api/v1/invitations",
        json={"email": "hrhire@acme.com", "role": "executive"},
        headers=auth_headers(settings, hr),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["email"] == "hrhire@acme.com"


async def test_admin_can_invite(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    resp = await client.post(
        "/api/v1/invitations",
        json={"email": "NewHire@acme.com", "role": "manager"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "newhire@acme.com"  # normalised
    assert body["role"] == "manager"
    assert "/invite/" in body["accept_url"]


async def test_accept_invite_provisions_employee(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await client.post(
        "/api/v1/invitations",
        json={"email": "grace@acme.com", "role": "senior_manager"},
        headers=auth_headers(settings, seed.admin),
    )
    token = created.json()["accept_url"].rsplit("/", 1)[1]

    # The invited person signs in (Better Auth) and accepts.
    resp = await client.post(
        "/api/v1/invitations/accept",
        json={"token": token},
        headers=bearer_for_email(settings, "grace@acme.com"),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "senior_manager"

    # They are now a real employee — a second accept is rejected (already used).
    reused = await client.post(
        "/api/v1/invitations/accept",
        json={"token": token},
        headers=bearer_for_email(settings, "grace@acme.com"),
    )
    assert reused.status_code == 409


async def test_accept_uses_real_name_from_identity(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The provisioned employee takes its name from the signed-in Google/identity
    profile, not an email-derived placeholder."""
    created = await client.post(
        "/api/v1/invitations",
        json={"email": "tech5@acme.com", "role": "employee"},
        headers=auth_headers(settings, seed.admin),
    )
    token = created.json()["accept_url"].rsplit("/", 1)[1]

    resp = await client.post(
        "/api/v1/invitations/accept",
        json={"token": token},
        headers=bearer_for_email(settings, "tech5@acme.com", name="Akshat Sharma"),
    )
    assert resp.status_code == 200
    # Real name, not the "Tech5" placeholder the email would have produced.
    assert resp.json()["full_name"] == "Akshat Sharma"


async def test_login_backfills_placeholder_name(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """An employee accepted without a name (placeholder) gets their real name on a
    later sign-in that carries one — but a real name is never overwritten."""
    created = await client.post(
        "/api/v1/invitations",
        json={"email": "tech7@acme.com", "role": "employee"},
        headers=auth_headers(settings, seed.admin),
    )
    token = created.json()["accept_url"].rsplit("/", 1)[1]

    # Accept with no name claim → placeholder "Tech7".
    accepted = await client.post(
        "/api/v1/invitations/accept",
        json={"token": token},
        headers=bearer_for_email(settings, "tech7@acme.com"),
    )
    assert accepted.json()["full_name"] == "Tech7"

    # Any authenticated request carrying the real name backfills it.
    me = await client.get(
        "/api/v1/employees/me",
        headers=bearer_for_email(settings, "tech7@acme.com", name="Priya Nair"),
    )
    assert me.status_code == 200
    assert me.json()["full_name"] == "Priya Nair"


async def test_admin_can_list_and_resend_pending(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await client.post(
        "/api/v1/invitations",
        json={"email": "pending@acme.com", "role": "manager"},
        headers=auth_headers(settings, seed.admin),
    )

    listed = await client.get("/api/v1/invitations", headers=auth_headers(settings, seed.admin))
    assert listed.status_code == 200
    pending = listed.json()
    assert any(i["email"] == "pending@acme.com" for i in pending)

    invite_id = next(i["id"] for i in pending if i["email"] == "pending@acme.com")
    resent = await client.post(
        f"/api/v1/invitations/{invite_id}/resend",
        headers=auth_headers(settings, seed.admin),
    )
    assert resent.status_code == 200
    assert "/invite/" in resent.json()["accept_url"]


async def test_non_admin_cannot_list_invitations(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get("/api/v1/invitations", headers=auth_headers(settings, seed.manager))
    assert resp.status_code == 403


async def test_accept_requires_matching_email(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await client.post(
        "/api/v1/invitations",
        json={"email": "intended@acme.com", "role": "employee"},
        headers=auth_headers(settings, seed.admin),
    )
    token = created.json()["accept_url"].rsplit("/", 1)[1]

    # A different signed-in account must not be able to claim someone else's invite.
    resp = await client.post(
        "/api/v1/invitations/accept",
        json={"token": token},
        headers=bearer_for_email(settings, "someone-else@acme.com"),
    )
    assert resp.status_code == 403

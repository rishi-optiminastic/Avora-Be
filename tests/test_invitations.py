"""Invitation flow — admin-only creation and accept-provisions-employee."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers, bearer_for_email


async def test_non_admin_cannot_invite(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/invitations",
        json={"email": "newhire@acme.com", "role": "manager"},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


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

"""Admin/HR user management — profile edit, activate/deactivate, and invite
revoke. Authorization is the point (CLAUDE.md §9): admin OR HR may do these;
everyone else gets 403 — and ROLE changes stay admin-only (rule 5.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.invitation import Invitation, InvitationStatus
from tests.conftest import _Seed, auth_headers


async def _add_hr(db: AsyncSession) -> Employee:
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


async def test_admin_and_hr_can_edit_profile_but_employee_cannot(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    hr = await _add_hr(db)
    url = f"/api/v1/employees/{seed.report.id}"
    body = {"full_name": "Remy Renamed", "department": "Platform", "job_title": "Staff Eng"}

    # Employee (the subject) cannot edit their own record via the admin path.
    assert (
        await client.patch(url, json=body, headers=auth_headers(settings, seed.report))
    ).status_code == 403
    # Outsider cannot.
    assert (
        await client.patch(url, json=body, headers=auth_headers(settings, seed.outsider))
    ).status_code == 403

    # Admin can.
    admin_resp = await client.patch(url, json=body, headers=auth_headers(settings, seed.admin))
    assert admin_resp.status_code == 200
    assert admin_resp.json()["full_name"] == "Remy Renamed"
    assert admin_resp.json()["department"] == "Platform"

    # HR can too.
    hr_resp = await client.patch(
        url, json={"full_name": "Remy HR-Edited"}, headers=auth_headers(settings, hr)
    )
    assert hr_resp.status_code == 200
    assert hr_resp.json()["full_name"] == "Remy HR-Edited"


async def test_admin_edits_hire_date_and_biometric_id(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    hr = await _add_hr(db)
    url = f"/api/v1/employees/{seed.report.id}"
    resp = await client.patch(
        url,
        json={
            "full_name": "Remy R",
            "hire_date": "2021-03-15",
            "biometric_id": "BIO-42",
        },
        headers=auth_headers(settings, hr),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hire_date"] == "2021-03-15"
    assert body["biometric_id"] == "BIO-42"


async def test_partial_patch_does_not_clobber_unsent_fields(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    url = f"/api/v1/employees/{seed.report.id}"
    # First set several fields together.
    seeded = await client.patch(
        url,
        json={
            "full_name": "Remy Original",
            "department": "Platform",
            "hire_date": "2020-01-02",
            "biometric_id": "BIO-7",
        },
        headers=auth_headers(settings, seed.admin),
    )
    assert seeded.status_code == 200

    # Now PATCH ONLY the name — the other fields must survive (PATCH, not replace).
    renamed = await client.patch(
        url, json={"full_name": "Remy Renamed"}, headers=auth_headers(settings, seed.admin)
    )
    assert renamed.status_code == 200
    body = renamed.json()
    assert body["full_name"] == "Remy Renamed"
    assert body["department"] == "Platform"
    assert body["hire_date"] == "2020-01-02"
    assert body["biometric_id"] == "BIO-7"

    # An explicit null DOES clear a field (so the editor can remove a value),
    # while a field that isn't sent in the same body stays untouched.
    cleared = await client.patch(
        url,
        json={"full_name": "Remy Renamed", "department": None},
        headers=auth_headers(settings, seed.admin),
    )
    assert cleared.status_code == 200
    assert cleared.json()["department"] is None
    assert cleared.json()["biometric_id"] == "BIO-7"  # still untouched


async def test_admin_cannot_demote_own_admin_role(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    # No self-lockout: the last/own admin can't strip their own admin role.
    resp = await client.put(
        f"/api/v1/employees/{seed.admin.id}/role",
        json={"role": "manager"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 422


async def test_admin_can_upload_another_avatar_but_employee_cannot(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    # 1x1 transparent PNG.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63fccfc0f01f00050001ff a2 a2 8d 0a 0000000049454e44ae426082".replace(
            " ", ""
        )
    )
    url = f"/api/v1/employees/{seed.report.id}/avatar"

    # A regular employee cannot set someone else's photo.
    forbidden = await client.post(
        url,
        content=png,
        headers={**auth_headers(settings, seed.outsider), "content-type": "image/png"},
    )
    assert forbidden.status_code == 403

    # Admin can.
    ok = await client.post(
        url,
        content=png,
        headers={**auth_headers(settings, seed.admin), "content-type": "image/png"},
    )
    assert ok.status_code == 201
    assert ok.json()["has_avatar"] is True


async def test_status_activate_deactivate(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    url = f"/api/v1/employees/{seed.report.id}/status"

    # Manager (not admin/HR) cannot.
    assert (
        await client.patch(
            url, json={"is_active": False}, headers=auth_headers(settings, seed.manager)
        )
    ).status_code == 403

    # Admin deactivates, then reactivates.
    off = await client.patch(
        url, json={"is_active": False}, headers=auth_headers(settings, seed.admin)
    )
    assert off.status_code == 200 and off.json()["is_active"] is False
    on = await client.patch(
        url, json={"is_active": True}, headers=auth_headers(settings, seed.admin)
    )
    assert on.status_code == 200 and on.json()["is_active"] is True


async def test_admin_cannot_deactivate_self(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    resp = await client.patch(
        f"/api/v1/employees/{seed.admin.id}/status",
        json={"is_active": False},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 422  # ValidationError — no self-lockout


async def test_role_change_stays_admin_only(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    hr = await _add_hr(db)
    url = f"/api/v1/employees/{seed.report.id}/role"
    # HR may manage users but NOT grant roles (rule 5.5).
    assert (
        await client.put(url, json={"role": "manager"}, headers=auth_headers(settings, hr))
    ).status_code == 403
    # Admin can.
    assert (
        await client.put(url, json={"role": "manager"}, headers=auth_headers(settings, seed.admin))
    ).status_code == 200


async def test_invitation_revoke(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    hr = await _add_hr(db)
    invite = Invitation(
        email="newhire@corp.test",
        role=Role.EXECUTIVE,
        token_hash="t" * 64,
        invited_by=seed.admin.id,
        status=InvitationStatus.PENDING,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db.add(invite)
    await db.commit()
    url = f"/api/v1/invitations/{invite.id}/revoke"

    # Employee cannot revoke.
    assert (await client.post(url, headers=auth_headers(settings, seed.report))).status_code == 403
    # HR can.
    ok = await client.post(url, headers=auth_headers(settings, hr))
    assert ok.status_code == 200
    assert ok.json()["status"] == "revoked"
    # Revoking again is a conflict (no longer pending).
    assert (await client.post(url, headers=auth_headers(settings, seed.admin))).status_code == 409


async def test_biometric_id_taken_by_another_active_employee_is_a_409(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """A device id already in use must come back as a named 409, not a crash.

    `uq_employees_active_biometric_id` enforces uniqueness in Postgres, so
    without this check the write reached the database and died there - a 500 (a
    503 through the proxy) with an empty body. The editor caught that and showed
    its generic "only admins/HR can manage users" line, so a duplicate id looked
    for months like HR lacking permission. Two people whose ids are swapped can
    only be fixed one at a time, and the message has to say so.
    """
    hr = await _add_hr(db)
    seed.report.biometric_id = "172"
    await db.commit()

    resp = await client.patch(
        f"/api/v1/employees/{seed.outsider.id}",
        json={"full_name": seed.outsider.full_name, "biometric_id": "172"},
        headers=auth_headers(settings, hr),
    )
    assert resp.status_code == 409
    # The holder is named, because that is the only way to know what to clear.
    assert seed.report.full_name in resp.json()["error"]["message"]

    # The other person's id is untouched by the refusal.
    await db.refresh(seed.outsider)
    assert seed.outsider.biometric_id != "172"


async def test_biometric_id_can_be_set_to_your_own_current_value(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """Re-saving the form without touching the id must not refuse itself - the
    holder of the id IS the person being edited."""
    hr = await _add_hr(db)
    seed.report.biometric_id = "172"
    await db.commit()

    resp = await client.patch(
        f"/api/v1/employees/{seed.report.id}",
        json={"full_name": "Remy R", "biometric_id": "172"},
        headers=auth_headers(settings, hr),
    )
    assert resp.status_code == 200
    assert resp.json()["biometric_id"] == "172"


async def test_biometric_id_freed_by_an_inactive_employee_is_reusable(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """Offboarding keeps the row (rule 8), and the unique index is partial on
    `is_active` for exactly that reason - a leaver must not hold an id hostage."""
    hr = await _add_hr(db)
    seed.report.biometric_id = "172"
    seed.report.is_active = False
    await db.commit()

    resp = await client.patch(
        f"/api/v1/employees/{seed.outsider.id}",
        json={"full_name": seed.outsider.full_name, "biometric_id": "172"},
        headers=auth_headers(settings, hr),
    )
    assert resp.status_code == 200
    assert resp.json()["biometric_id"] == "172"

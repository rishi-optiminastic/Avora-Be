"""Device authorization tests — enroll/revoke = admin/IT only; reads are scoped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.activity import ActivitySample
from app.models.screenshot import Screenshot
from tests.conftest import _Seed, auth_headers


async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/devices")).status_code == 401


async def test_admin_can_enroll_returns_token_once(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/devices",
        json={"employee_id": str(seed.outsider.id), "label": "olive-mbp"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["employee_id"] == str(seed.outsider.id)
    assert body["token"]  # raw token shown once


async def test_non_admin_cannot_enroll(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/devices",
        json={"employee_id": str(seed.report.id), "label": "x"},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_list_is_scoped(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    # The seed enrolls one device for the report.
    mgr = await client.get("/api/v1/devices", headers=auth_headers(settings, seed.manager))
    assert mgr.status_code == 200
    assert any(d["employee_id"] == str(seed.report.id) for d in mgr.json())

    outsider = await client.get("/api/v1/devices", headers=auth_headers(settings, seed.outsider))
    assert outsider.json() == []


async def test_self_enroll_binds_to_caller_not_client_id(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Any authenticated employee may self-enroll — and even if they smuggle in a
    # foreign employee_id, the device binds to the caller (Golden rule #2).
    resp = await client.post(
        "/api/v1/devices/self-enroll",
        json={"hostname": "olive-mbp", "os": "macOS 15", "employee_id": str(seed.admin.id)},
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["employee_id"] == str(seed.outsider.id)
    assert body["token"]  # raw token shown once
    assert body["label"] == "olive-mbp · macOS 15"


async def test_self_enroll_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.post("/api/v1/devices/self-enroll", json={"hostname": "x"})
    assert resp.status_code == 401


async def test_admin_can_revoke_non_admin_cannot(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    denied = await client.post(
        f"/api/v1/devices/{seed.device.id}/revoke", headers=auth_headers(settings, seed.manager)
    )
    assert denied.status_code == 403

    ok = await client.post(
        f"/api/v1/devices/{seed.device.id}/revoke", headers=auth_headers(settings, seed.admin)
    )
    assert ok.status_code == 200
    assert ok.json()["is_revoked"] is True


# --------------------------------------------------------------------------- #
# Reassign — a device is bound to its owner once, at enrollment, from whoever was
# signed in to Avora in that machine's browser. When a laptop changes hands
# nothing re-checks it, so the new user's screenshots and activity keep filing
# under the previous owner. These pin the only way to correct that.
# --------------------------------------------------------------------------- #


async def test_non_admin_cannot_reassign(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # The manager can SEE this device (it belongs to their report), which is
    # exactly why seeing it must not imply being able to re-point it.
    resp = await client.post(
        f"/api/v1/devices/{seed.device.id}/reassign",
        json={"employee_id": str(seed.outsider.id)},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_reassign_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.post(
        f"/api/v1/devices/{seed.device.id}/reassign",
        json={"employee_id": str(seed.outsider.id)},
    )
    assert resp.status_code == 401


async def test_admin_reassigns_device_to_its_real_user(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        f"/api/v1/devices/{seed.device.id}/reassign",
        json={"employee_id": str(seed.outsider.id)},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device"]["employee_id"] == str(seed.outsider.id)
    assert body["previous_employee_id"] == str(seed.report.id)
    # History untouched unless explicitly asked for.
    assert body["screenshots_moved"] == 0
    assert body["activity_samples_moved"] == 0

    listed = await client.get("/api/v1/devices", headers=auth_headers(settings, seed.admin))
    assert any(
        d["id"] == str(seed.device.id) and d["employee_id"] == str(seed.outsider.id)
        for d in listed.json()
    )


async def test_reassign_to_unknown_employee_is_404(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    import uuid as _uuid

    resp = await client.post(
        f"/api/v1/devices/{seed.device.id}/reassign",
        json={"employee_id": str(_uuid.uuid4())},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 404


async def test_reassign_unknown_device_is_404(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    import uuid as _uuid

    resp = await client.post(
        f"/api/v1/devices/{_uuid.uuid4()}/reassign",
        json={"employee_id": str(seed.outsider.id)},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 404


async def test_reassign_to_the_same_employee_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # A no-op would otherwise write a misleading audit row claiming a change.
    resp = await client.post(
        f"/api/v1/devices/{seed.device.id}/reassign",
        json={"employee_id": str(seed.report.id)},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code in (400, 422)


async def test_history_move_respects_the_handover_date(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Only what was captured AFTER the machine changed hands moves.

    A blanket re-file would hand the previous owner's work to the new one for a
    period when it genuinely was theirs - and because activity samples feed
    attendance when there is no biometric punch, that rewrites both people's
    records for days they really did work.
    """
    handover = datetime.now(UTC) - timedelta(days=7)
    before, after = handover - timedelta(days=3), handover + timedelta(days=3)

    for stamp in (before, after):
        db.add(
            Screenshot(
                device_id=seed.device.id,
                employee_id=seed.report.id,
                captured_at=stamp,
                received_at=stamp,
                content_type="image/jpeg",
                image=b"x",
            )
        )
    for i, stamp in enumerate((before, after)):
        db.add(
            ActivitySample(
                device_id=seed.device.id,
                employee_id=seed.report.id,
                sequence=900 + i,
                client_timestamp=stamp,
                received_at=stamp,
                idle_seconds=0,
            )
        )
    await db.commit()

    resp = await client.post(
        f"/api/v1/devices/{seed.device.id}/reassign",
        json={
            "employee_id": str(seed.outsider.id),
            "history_from": handover.isoformat(),
        },
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["screenshots_moved"] == 1, "only the post-handover screenshot moves"
    assert body["activity_samples_moved"] == 1, "only the post-handover sample moves"

    # And the pre-handover rows still belong to the person who really did that work.
    kept = await db.execute(select(Screenshot.employee_id).where(Screenshot.received_at == before))
    assert kept.scalar_one() == seed.report.id

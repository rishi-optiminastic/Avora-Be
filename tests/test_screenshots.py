"""Screenshot upload + scoped reads, and image content-type validation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import compute_hmac_sha256
from app.models.screenshot import Screenshot
from tests.conftest import _Seed, allow_capture, auth_headers

IMG = b"\xff\xd8\xff\xe0fake-jpeg-bytes-for-test"


def _shot_headers(
    raw_token: str,
    image: bytes,
    content_type: str = "image/jpeg",
    width: str = "1280",
    height: str = "800",
    monitors: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {raw_token}",
        "X-Signature": compute_hmac_sha256(raw_token, image),
        "Content-Type": content_type,
        "X-Width": width,
        "X-Height": height,
    }
    if monitors is not None:
        headers["X-Monitors"] = monitors
    return headers


async def _upload(
    client: AsyncClient, seed: _Seed, image: bytes = IMG, content_type: str = "image/jpeg"
) -> object:
    return await client.post(
        "/api/v1/screenshots",
        content=image,
        headers=_shot_headers(seed.device_raw_token, image, content_type),
    )


async def test_screenshots_unauthenticated(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/screenshots")).status_code == 401


async def test_delete_is_admin_only(
    client: AsyncClient, settings: Settings, db: AsyncSession, seed: _Seed
) -> None:
    """Only an admin may delete a screenshot; the deleted row is then gone."""
    await allow_capture(db, seed.report.id)
    up = await _upload(client, seed)
    shot_id = up.json()["id"]  # type: ignore[attr-defined]

    denied = await client.delete(
        f"/api/v1/screenshots/{shot_id}", headers=auth_headers(settings, seed.report)
    )
    assert denied.status_code == 403

    ok = await client.delete(
        f"/api/v1/screenshots/{shot_id}", headers=auth_headers(settings, seed.admin)
    )
    assert ok.status_code == 204
    gone = await db.get(Screenshot, uuid.UUID(shot_id))
    assert gone is None


async def test_upload_stores_per_monitor_rects(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    """A dual-monitor capture's X-Monitors header is parsed, validated against the
    image bounds, and stored so the OCR worker can crop per screen."""
    await allow_capture(db, seed.report.id)
    resp = await client.post(
        "/api/v1/screenshots",
        content=IMG,
        headers=_shot_headers(
            seed.device_raw_token,
            IMG,
            width="2560",
            height="800",
            monitors="0,0,1280,800;1280,0,1280,800",
        ),
    )
    assert resp.status_code == 202, resp.text
    shot = (
        await db.execute(select(Screenshot).where(Screenshot.id == uuid.UUID(resp.json()["id"])))
    ).scalar_one()
    assert shot.monitors == [[0, 0, 1280, 800], [1280, 0, 1280, 800]]


async def test_upload_drops_malformed_monitors(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    """Garbage / out-of-bounds rects are dropped (agent is untrusted); a clean
    capture with no usable rects stores an empty list → whole-image OCR."""
    await allow_capture(db, seed.report.id)
    resp = await client.post(
        "/api/v1/screenshots",
        content=IMG,
        headers=_shot_headers(
            seed.device_raw_token,
            IMG,
            width="1280",
            height="800",
            monitors="garbage;1,2,3;9999,0,100,100",  # bad shape, bad shape, off-image
        ),
    )
    assert resp.status_code == 202, resp.text
    shot = (
        await db.execute(select(Screenshot).where(Screenshot.id == uuid.UUID(resp.json()["id"])))
    ).scalar_one()
    assert shot.monitors == []


async def test_upload_bad_hmac_rejected(client: AsyncClient, seed: _Seed) -> None:
    headers = _shot_headers(seed.device_raw_token, IMG)
    headers["X-Signature"] = "deadbeef"
    resp = await client.post("/api/v1/screenshots", content=IMG, headers=headers)
    assert resp.status_code == 401


async def test_upload_rejects_bad_type(client: AsyncClient, db: AsyncSession, seed: _Seed) -> None:
    await allow_capture(db, seed.report.id)
    resp = await _upload(client, seed, content_type="text/html")
    assert resp.status_code == 422


async def test_upload_list_and_fetch_are_scoped(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await allow_capture(db, seed.report.id)
    up = await _upload(client, seed)
    assert up.status_code == 202, up.text
    shot = up.json()
    assert shot["employee_id"] == str(seed.report.id)
    assert shot["byte_size"] == len(IMG)

    # Manager sees the report's screenshot in the scoped list.
    listed = await client.get("/api/v1/screenshots", headers=auth_headers(settings, seed.manager))
    assert listed.status_code == 200
    assert shot["id"] in [s["id"] for s in listed.json()]

    # Outsider's scoped list never includes the report.
    outsider = await client.get(
        "/api/v1/screenshots", headers=auth_headers(settings, seed.outsider)
    )
    assert all(s["employee_id"] != str(seed.report.id) for s in outsider.json())

    # Image bytes round-trip for the manager…
    img = await client.get(
        f"/api/v1/screenshots/{shot['id']}", headers=auth_headers(settings, seed.manager)
    )
    assert img.status_code == 200
    assert img.content == IMG
    assert img.headers["content-type"].startswith("image/jpeg")

    # …but an out-of-scope caller gets 404 (never leaks existence).
    blocked = await client.get(
        f"/api/v1/screenshots/{shot['id']}", headers=auth_headers(settings, seed.outsider)
    )
    assert blocked.status_code == 404


# --- the date filter -------------------------------------------------------- #
async def _seed_on(db: AsyncSession, employee_id: object, when: datetime, device_id: object) -> str:
    """A screenshot stamped at `when`, bypassing upload so the date is controlled."""
    from app.models.screenshot import Screenshot

    shot = Screenshot(
        device_id=device_id,
        employee_id=employee_id,
        captured_at=when,
        received_at=when,
        width=100,
        height=100,
        byte_size=len(IMG),
        image=IMG,
        content_type="image/jpeg",
    )
    db.add(shot)
    await db.commit()
    return str(shot.id)


async def test_day_filter_returns_only_that_day(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    now = datetime.now(UTC)
    today = await _seed_on(db, seed.report.id, now - timedelta(hours=2), seed.device.id)
    yesterday = await _seed_on(db, seed.report.id, now - timedelta(days=1, hours=2), seed.device.id)

    res = await client.get(
        f"/api/v1/screenshots?day={now.date().isoformat()}",
        headers=auth_headers(settings, seed.admin),
    )

    assert res.status_code == 200
    ids = [s["id"] for s in res.json()]
    assert today in ids
    assert yesterday not in ids


async def test_no_day_returns_the_newest_across_days(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    now = datetime.now(UTC)
    today = await _seed_on(db, seed.report.id, now - timedelta(hours=2), seed.device.id)
    yesterday = await _seed_on(db, seed.report.id, now - timedelta(days=1, hours=2), seed.device.id)

    res = await client.get("/api/v1/screenshots", headers=auth_headers(settings, seed.admin))

    ids = [s["id"] for s in res.json()]
    assert today in ids and yesterday in ids
    assert ids.index(today) < ids.index(yesterday)  # newest first


async def test_a_day_outside_the_browse_window_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The window is a real limit, not a UI convenience: hand-editing the query
    must not turn the filter into an export of the whole retention period."""
    old = (datetime.now(UTC).date() - timedelta(days=10)).isoformat()

    res = await client.get(
        f"/api/v1/screenshots?day={old}", headers=auth_headers(settings, seed.admin)
    )

    assert res.status_code == 422


async def test_a_future_day_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    ahead = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()

    res = await client.get(
        f"/api/v1/screenshots?day={ahead}", headers=auth_headers(settings, seed.admin)
    )

    assert res.status_code == 422


async def test_the_day_filter_cannot_widen_scope(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Filtering narrows what a caller already sees. An outsider asking for a
    specific day must still not see the report's screenshots."""
    now = datetime.now(UTC)
    mine = await _seed_on(db, seed.report.id, now - timedelta(hours=1), seed.device.id)

    res = await client.get(
        f"/api/v1/screenshots?day={now.date().isoformat()}",
        headers=auth_headers(settings, seed.outsider),
    )

    assert res.status_code == 200
    assert mine not in [s["id"] for s in res.json()]


async def test_offset_pages_without_repeating(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    now = datetime.now(UTC)
    for i in range(5):
        await _seed_on(db, seed.report.id, now - timedelta(minutes=i), seed.device.id)

    first = await client.get(
        "/api/v1/screenshots?limit=2", headers=auth_headers(settings, seed.admin)
    )
    second = await client.get(
        "/api/v1/screenshots?limit=2&offset=2", headers=auth_headers(settings, seed.admin)
    )

    a = [s["id"] for s in first.json()]
    b = [s["id"] for s in second.json()]
    assert len(a) == 2 and len(b) == 2
    assert not set(a) & set(b)  # no overlap between pages


# --- who may delete --------------------------------------------------------- #
async def _set_role(db: AsyncSession, employee_id: object, role: str) -> None:
    from app.models.employee import Employee, Role

    person = await db.get(Employee, employee_id)
    assert person is not None
    person.role = Role(role)
    await db.commit()


async def test_it_admin_can_delete(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """`it_admin` is a full admin for authorization, so it deletes like one.
    The UI reads the same effective role from /me, so the button matches."""
    await allow_capture(db, seed.report.id)
    shot_id = (await _upload(client, seed)).json()["id"]
    await _set_role(db, seed.outsider.id, "it_admin")

    res = await client.delete(
        f"/api/v1/screenshots/{shot_id}", headers=auth_headers(settings, seed.outsider)
    )

    assert res.status_code == 204


async def test_hr_cannot_delete(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The one worth pinning: HR reads everything else in the org, so it is the
    likely accident. Deleting a screenshot stays admin and IT-admin only."""
    await allow_capture(db, seed.report.id)
    shot_id = (await _upload(client, seed)).json()["id"]
    await _set_role(db, seed.outsider.id, "hr")

    res = await client.delete(
        f"/api/v1/screenshots/{shot_id}", headers=auth_headers(settings, seed.outsider)
    )

    assert res.status_code == 403


async def test_a_manager_cannot_delete(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await allow_capture(db, seed.report.id)
    shot_id = (await _upload(client, seed)).json()["id"]

    res = await client.delete(
        f"/api/v1/screenshots/{shot_id}", headers=auth_headers(settings, seed.manager)
    )

    assert res.status_code == 403

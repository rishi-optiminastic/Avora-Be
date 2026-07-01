"""Screenshot upload + scoped reads, and image content-type validation."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import compute_hmac_sha256
from app.models.screenshot import Screenshot
from tests.conftest import _Seed, auth_headers

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


async def test_upload_stores_per_monitor_rects(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    """A dual-monitor capture's X-Monitors header is parsed, validated against the
    image bounds, and stored so the OCR worker can crop per screen."""
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


async def test_upload_rejects_bad_type(client: AsyncClient, seed: _Seed) -> None:
    resp = await _upload(client, seed, content_type="text/html")
    assert resp.status_code == 422


async def test_upload_list_and_fetch_are_scoped(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
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

"""Workspace links + per-entry visibility (department / individual access)."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers

_SHEET = "https://docs.google.com/spreadsheets/d/abc123/edit"


async def _create_link(
    client: AsyncClient, settings: Settings, actor: object, body: dict[str, object]
) -> dict[str, object]:
    resp = await client.post(
        "/api/v1/workspace/files/link",
        json={"name": "Budget", "url": _SHEET, "category": "reference", **body},
        headers=auth_headers(settings, actor),  # type: ignore[arg-type]
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_link_stores_url(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    created = await _create_link(client, settings, seed.report, {})
    assert created["url"] == _SHEET
    assert created["byte_size"] == 0
    assert created["visibility"] == "everyone"


async def test_link_rejects_non_http_url(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/workspace/files/link",
        json={"name": "x", "url": "javascript:alert(1)"},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422


async def test_restricted_entry_is_hidden_from_others(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Manager shares a restricted link with the report only.
    created = await _create_link(
        client,
        settings,
        seed.manager,
        {"visibility": "restricted", "visible_employee_ids": [str(seed.report.id)]},
    )
    assert created["visibility"] == "restricted"
    file_id = created["id"]

    # The targeted report sees it; the uploader (manager) sees it; admin sees it.
    for actor in (seed.report, seed.manager, seed.admin):
        listed = await client.get("/api/v1/workspace/files", headers=auth_headers(settings, actor))
        assert file_id in [f["id"] for f in listed.json()], actor

    # An unrelated employee does NOT see it in the list...
    outsider_list = await client.get(
        "/api/v1/workspace/files", headers=auth_headers(settings, seed.outsider)
    )
    assert file_id not in [f["id"] for f in outsider_list.json()]

    # ...and cannot reach it directly (404, never leaking that it exists).
    denied = await client.get(
        f"/api/v1/workspace/files/{file_id}/download",
        headers=auth_headers(settings, seed.outsider),
    )
    assert denied.status_code == 404

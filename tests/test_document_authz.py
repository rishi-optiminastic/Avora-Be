"""Employee document authorization — HR/Admin manage; person reads own; else nothing."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers

_DOC = {"title": "Offer letter", "category": "contract", "url": "https://drive.example/offer.pdf"}


async def test_admin_can_add_list_delete(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await client.post(
        f"/api/v1/employees/{seed.report.id}/documents",
        json=_DOC,
        headers=auth_headers(settings, seed.admin),
    )
    assert created.status_code == 201
    doc_id = created.json()["id"]

    listed = await client.get(
        f"/api/v1/employees/{seed.report.id}/documents",
        headers=auth_headers(settings, seed.admin),
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    removed = await client.delete(
        f"/api/v1/employees/{seed.report.id}/documents/{doc_id}",
        headers=auth_headers(settings, seed.admin),
    )
    assert removed.status_code == 204


async def test_person_reads_own_but_cannot_add(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    own = await client.get(
        f"/api/v1/employees/{seed.report.id}/documents",
        headers=auth_headers(settings, seed.report),
    )
    assert own.status_code == 200

    forbidden = await client.post(
        f"/api/v1/employees/{seed.report.id}/documents",
        json=_DOC,
        headers=auth_headers(settings, seed.report),
    )
    assert forbidden.status_code == 403


async def test_manager_cannot_read_reports_documents(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get(
        f"/api/v1/employees/{seed.report.id}/documents",
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_rejects_non_http_url(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    resp = await client.post(
        f"/api/v1/employees/{seed.report.id}/documents",
        json={"title": "x", "category": "other", "url": "javascript:alert(1)"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 422

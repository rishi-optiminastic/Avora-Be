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


async def test_upload_file_admin_only_and_downloadable_by_self(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = b"%PDF-1.4 fake offer letter"
    params = {"title": "Signed offer", "category": "contract", "filename": "offer.pdf"}

    # The employee themselves cannot upload (writes are HR/Admin only).
    denied = await client.post(
        f"/api/v1/employees/{seed.report.id}/documents/upload",
        params=params,
        content=body,
        headers={**auth_headers(settings, seed.report), "Content-Type": "application/pdf"},
    )
    assert denied.status_code == 403

    # Admin uploads the file; it comes back as a byte-backed doc (no url).
    created = await client.post(
        f"/api/v1/employees/{seed.report.id}/documents/upload",
        params=params,
        content=body,
        headers={**auth_headers(settings, seed.admin), "Content-Type": "application/pdf"},
    )
    assert created.status_code == 201, created.text
    doc = created.json()
    assert doc["url"] is None
    assert doc["byte_size"] == len(body)
    doc_id = doc["id"]

    # The person can download their own uploaded document...
    ok = await client.get(
        f"/api/v1/employees/{seed.report.id}/documents/{doc_id}/download",
        headers=auth_headers(settings, seed.report),
    )
    assert ok.status_code == 200
    assert ok.content == body

    # ...but an unrelated employee gets a 404 (never leaking it exists).
    denied_dl = await client.get(
        f"/api/v1/employees/{seed.report.id}/documents/{doc_id}/download",
        headers=auth_headers(settings, seed.outsider),
    )
    assert denied_dl.status_code == 404


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

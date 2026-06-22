"""Workspace file authorization + behavior.

The workspace is a *shared* drive: any signed-in employee can upload, list, and
download (org-wide read). The only restricted action is delete — uploader or
HR/Admin only — so that's where the authz tests bite (Testing §9).
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.work_entity import WorkEntity
from app.services.workspace_file_service import MAX_FILE_BYTES
from tests.conftest import _Seed, auth_headers


def _upload_headers(settings: Settings, employee: object) -> dict[str, str]:
    return {
        **auth_headers(settings, employee),  # type: ignore[arg-type]
        "Content-Type": "text/plain",
    }


async def _upload(
    client: AsyncClient,
    settings: Settings,
    employee: object,
    *,
    name: str = "Sprint notes",
    body: bytes = b"hello workspace",
    params: dict[str, str] | None = None,
) -> dict[str, object]:
    resp = await client.post(
        "/api/v1/workspace/files",
        params={"name": name, "category": "brief", "filename": "notes.txt", **(params or {})},
        content=body,
        headers=_upload_headers(settings, employee),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_employee_can_upload_list_and_download(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await _upload(client, settings, seed.report)
    assert created["uploader_name"] == "Remy Report"
    assert created["byte_size"] == len(b"hello workspace")

    listed = await client.get(
        "/api/v1/workspace/files", headers=auth_headers(settings, seed.report)
    )
    assert listed.status_code == 200
    assert [f["id"] for f in listed.json()] == [created["id"]]

    download = await client.get(
        f"/api/v1/workspace/files/{created['id']}/download",
        headers=auth_headers(settings, seed.report),
    )
    assert download.status_code == 200
    assert download.content == b"hello workspace"


async def test_read_is_org_wide(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    """An unrelated employee can browse + download a shared file (by design)."""
    created = await _upload(client, settings, seed.report)

    listed = await client.get(
        "/api/v1/workspace/files", headers=auth_headers(settings, seed.outsider)
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    download = await client.get(
        f"/api/v1/workspace/files/{created['id']}/download",
        headers=auth_headers(settings, seed.outsider),
    )
    assert download.status_code == 200


async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/workspace/files")).status_code == 401


async def test_uploader_can_delete_own(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await _upload(client, settings, seed.report)
    removed = await client.delete(
        f"/api/v1/workspace/files/{created['id']}",
        headers=auth_headers(settings, seed.report),
    )
    assert removed.status_code == 204


async def test_non_uploader_cannot_delete(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await _upload(client, settings, seed.report)
    forbidden = await client.delete(
        f"/api/v1/workspace/files/{created['id']}",
        headers=auth_headers(settings, seed.outsider),
    )
    assert forbidden.status_code == 403

    # ...and the file is still there.
    listed = await client.get(
        "/api/v1/workspace/files", headers=auth_headers(settings, seed.report)
    )
    assert len(listed.json()) == 1


async def test_admin_can_delete_any(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await _upload(client, settings, seed.report)
    removed = await client.delete(
        f"/api/v1/workspace/files/{created['id']}",
        headers=auth_headers(settings, seed.admin),
    )
    assert removed.status_code == 204


async def test_empty_file_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/workspace/files",
        params={"name": "Empty", "category": "other"},
        content=b"",
        headers=_upload_headers(settings, seed.report),
    )
    assert resp.status_code == 422


async def test_unknown_project_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/workspace/files",
        params={
            "name": "Misfiled",
            "category": "brief",
            "project_id": "00000000-0000-0000-0000-000000000000",
        },
        content=b"data",
        headers=_upload_headers(settings, seed.report),
    )
    assert resp.status_code == 404


async def test_links_to_project_and_filters(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    project = WorkEntity(name="Acme Redesign", department="Design")
    db.add(project)
    await db.commit()

    created = await _upload(
        client, settings, seed.report, params={"project_id": str(project.id)}
    )
    assert created["project_id"] == str(project.id)
    assert created["project_name"] == "Acme Redesign"

    filtered = await client.get(
        "/api/v1/workspace/files",
        params={"project_id": str(project.id)},
        headers=auth_headers(settings, seed.report),
    )
    assert [f["id"] for f in filtered.json()] == [created["id"]]

    stats = await client.get(
        "/api/v1/workspace/files/stats", headers=auth_headers(settings, seed.report)
    )
    assert stats.status_code == 200
    body = stats.json()
    assert body["total_files"] == 1
    assert body["by_category"]["brief"] == 1


# --- security hardening ---------------------------------------------------- #
async def test_oversized_upload_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # A body over the cap is refused (413) — and via the Content-Length pre-check
    # so it never buffers fully into memory.
    resp = await client.post(
        "/api/v1/workspace/files",
        params={"name": "huge", "category": "other", "filename": "huge.bin"},
        content=b"x" * (MAX_FILE_BYTES + 1),
        headers=_upload_headers(settings, seed.report),
    )
    assert resp.status_code == 413


async def test_dangerous_mime_is_neutralized(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # An uploaded text/html is stored as a neutral type so it can't run inline.
    resp = await client.post(
        "/api/v1/workspace/files",
        params={"name": "x", "category": "other", "filename": "x.html"},
        content=b"<script>alert(1)</script>",
        headers={**auth_headers(settings, seed.report), "Content-Type": "text/html"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["content_type"] == "application/octet-stream"


async def test_download_forces_attachment(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await _upload(client, settings, seed.report)
    resp = await client.get(
        f"/api/v1/workspace/files/{created['id']}/download",
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-type"] == "application/octet-stream"


async def test_upload_is_rate_limited(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # The per-user upload cap is 20/min — a tight loop is throttled with 429.
    statuses = []
    for i in range(22):
        resp = await client.post(
            "/api/v1/workspace/files",
            params={"name": f"f{i}", "category": "other", "filename": f"f{i}.txt"},
            content=b"data",
            headers=_upload_headers(settings, seed.report),
        )
        statuses.append(resp.status_code)
    assert 429 in statuses

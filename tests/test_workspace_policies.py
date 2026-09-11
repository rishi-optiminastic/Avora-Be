"""Company policies published into the workspace store.

The workspace is otherwise a team drive anyone can add to, which is right for
briefs and assets. A policy is not: it speaks for the company, so a convincing
fake "Dress code v2" from any employee would be believed. Publishing and
withdrawing are HR/Admin only; READING stays open to everyone, which is the
entire point of publishing one.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


async def _publish(
    client: AsyncClient,
    settings: Settings,
    employee: object,
    *,
    name: str = "Leave Policy 2026",
    category: str = "policy",
):
    return await client.post(
        "/api/v1/workspace/files",
        params={"name": name, "category": category, "filename": "policy.pdf"},
        content=b"%PDF-1.4 leave policy",
        headers={
            **auth_headers(settings, employee),  # type: ignore[arg-type]
            "Content-Type": "application/pdf",
        },
    )


async def test_hr_can_publish_a_policy(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await _publish(client, settings, seed.admin)

    assert res.status_code == 201, res.text
    assert res.json()["category"] == "policy"


async def test_an_employee_cannot_publish_a_policy(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The whole reason the category is restricted."""
    res = await _publish(client, settings, seed.report)

    assert res.status_code == 403


async def test_a_manager_cannot_publish_a_policy(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await _publish(client, settings, seed.manager)

    assert res.status_code == 403


async def test_an_employee_can_still_upload_a_normal_file(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The restriction is on POLICY only - the team drive stays open."""
    res = await _publish(client, settings, seed.report, category="brief")

    assert res.status_code == 201


async def test_everyone_can_read_and_download_a_policy(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = (await _publish(client, settings, seed.admin)).json()

    listed = await client.get(
        "/api/v1/workspace/files",
        params={"category": "policy"},
        headers=auth_headers(settings, seed.report),
    )
    assert listed.status_code == 200
    assert [f["id"] for f in listed.json()] == [created["id"]]

    download = await client.get(
        f"/api/v1/workspace/files/{created['id']}/download",
        headers=auth_headers(settings, seed.report),
    )
    assert download.status_code == 200
    assert download.content == b"%PDF-1.4 leave policy"


async def test_an_employee_cannot_withdraw_a_policy(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Withdrawing one is as much a company act as publishing it."""
    created = (await _publish(client, settings, seed.admin)).json()

    res = await client.delete(
        f"/api/v1/workspace/files/{created['id']}",
        headers=auth_headers(settings, seed.report),
    )

    assert res.status_code == 403


async def test_hr_can_withdraw_a_policy(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = (await _publish(client, settings, seed.admin)).json()

    res = await client.delete(
        f"/api/v1/workspace/files/{created['id']}",
        headers=auth_headers(settings, seed.admin),
    )

    assert res.status_code == 204

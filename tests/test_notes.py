"""Note authz: a quick note is private to its author, full stop (§9)."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


async def test_list_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/notes")).status_code == 401


async def test_create_and_list_roundtrip(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    create = await client.post(
        "/api/v1/notes",
        json={"body": "Call the vendor back"},
        headers=auth_headers(settings, seed.report),
    )
    assert create.status_code == 201, create.text
    assert create.json()["body"] == "Call the vendor back"

    mine = await client.get("/api/v1/notes", headers=auth_headers(settings, seed.report))
    assert mine.status_code == 200
    assert [n["body"] for n in mine.json()] == ["Call the vendor back"]


async def test_notes_are_private_to_the_author(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await client.post(
        "/api/v1/notes", json={"body": "Secret plan"}, headers=auth_headers(settings, seed.report)
    )

    # An unrelated employee's own list is empty — they never see the report's note.
    theirs = await client.get("/api/v1/notes", headers=auth_headers(settings, seed.outsider))
    assert theirs.json() == []


async def test_outsider_cannot_delete_anothers_note(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await client.post(
        "/api/v1/notes", json={"body": "Don't forget"}, headers=auth_headers(settings, seed.report)
    )
    note_id = created.json()["id"]

    resp = await client.delete(
        f"/api/v1/notes/{note_id}", headers=auth_headers(settings, seed.outsider)
    )
    assert resp.status_code == 404

    # Still there for the owner.
    mine = await client.get("/api/v1/notes", headers=auth_headers(settings, seed.report))
    assert len(mine.json()) == 1


async def test_owner_can_delete_own_note(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await client.post(
        "/api/v1/notes", json={"body": "Temporary"}, headers=auth_headers(settings, seed.report)
    )
    note_id = created.json()["id"]

    resp = await client.delete(
        f"/api/v1/notes/{note_id}", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 204

    mine = await client.get("/api/v1/notes", headers=auth_headers(settings, seed.report))
    assert mine.json() == []

"""Env Sync: push/pull/history, conflict (409), no-op, PAT auth, and membership
scope. Every protected path has an out-of-scope (404/403) assertion (Testing §9).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers

pytestmark = pytest.mark.asyncio


async def _create_project(
    client: AsyncClient, headers: dict[str, str], name: str = "ranking-be"
) -> str:
    res = await client.post("/api/v1/envsync/projects", json={"name": name}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def test_create_project_appears_in_list_as_owner(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    h = auth_headers(settings, seed.admin)
    project_id = await _create_project(client, h)

    res = await client.get("/api/v1/envsync/projects", headers=h)
    assert res.status_code == 200
    projects = res.json()
    assert [p["id"] for p in projects] == [project_id]
    assert projects[0]["role"] == "owner"


async def test_push_pull_roundtrip_and_history(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    h = auth_headers(settings, seed.admin)
    pid = await _create_project(client, h)

    # First push (no base — empty project).
    r1 = await client.post(
        f"/api/v1/envsync/projects/{pid}/env",
        json={"content": "A=1\n", "base_version": None, "environment": "default"},
        headers=h,
    )
    assert r1.status_code == 201, r1.text
    v1 = r1.json()
    assert v1["content"] == "A=1\n"

    # Pull returns the same plaintext (proves encrypt→decrypt round-trips).
    pull = await client.get(f"/api/v1/envsync/projects/{pid}/env", headers=h)
    assert pull.status_code == 200
    assert pull.json()["version_id"] == v1["version_id"]
    assert pull.json()["content"] == "A=1\n"

    # Second push based on v1.
    r2 = await client.post(
        f"/api/v1/envsync/projects/{pid}/env",
        json={"content": "A=2\n", "base_version": v1["version_id"]},
        headers=h,
    )
    assert r2.status_code == 201, r2.text

    hist = await client.get(f"/api/v1/envsync/projects/{pid}/env/history", headers=h)
    assert hist.status_code == 200
    versions = hist.json()
    assert len(versions) == 2
    # newest first
    assert versions[0]["version_id"] == r2.json()["version_id"]

    envs = await client.get(f"/api/v1/envsync/projects/{pid}/environments", headers=h)
    assert envs.status_code == 200
    assert [e["environment"] for e in envs.json()] == ["default"]


async def test_push_noop_returns_200(client: AsyncClient, seed: _Seed, settings: Settings) -> None:
    h = auth_headers(settings, seed.admin)
    pid = await _create_project(client, h)
    r1 = await client.post(
        f"/api/v1/envsync/projects/{pid}/env",
        json={"content": "A=1\n", "base_version": None},
        headers=h,
    )
    head_id = r1.json()["version_id"]
    # Identical content based on head → no-op, 200, same version id.
    r2 = await client.post(
        f"/api/v1/envsync/projects/{pid}/env",
        json={"content": "A=1\n", "base_version": head_id},
        headers=h,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["version_id"] == head_id


async def test_push_conflict_returns_409_with_head(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    h = auth_headers(settings, seed.admin)
    pid = await _create_project(client, h)
    await client.post(
        f"/api/v1/envsync/projects/{pid}/env",
        json={"content": "A=1\n", "base_version": None},
        headers=h,
    )
    # Stale push: base_version None while a head exists → conflict.
    conflict = await client.post(
        f"/api/v1/envsync/projects/{pid}/env",
        json={"content": "A=99\n", "base_version": None},
        headers=h,
    )
    assert conflict.status_code == 409, conflict.text
    body = conflict.json()
    assert body["head"]["content"] == "A=1\n"  # extension uses this to resolve


async def test_personal_access_token_authenticates(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    h = auth_headers(settings, seed.admin)
    pid = await _create_project(client, h)

    tok = await client.post("/api/v1/envsync/tokens", json={"label": "vscode"}, headers=h)
    assert tok.status_code == 201, tok.text
    raw = tok.json()["token"]
    assert raw  # returned once

    # The extension uses the PAT as a bearer — no JWT.
    pat_headers = {"Authorization": f"Bearer {raw}"}
    res = await client.get("/api/v1/envsync/projects", headers=pat_headers)
    assert res.status_code == 200
    assert [p["id"] for p in res.json()] == [pid]

    # Revoked token no longer authenticates.
    tid = tok.json()["id"]
    assert (await client.delete(f"/api/v1/envsync/tokens/{tid}", headers=h)).status_code == 204
    assert (await client.get("/api/v1/envsync/projects", headers=pat_headers)).status_code == 401


async def test_non_member_cannot_see_or_push(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    owner = auth_headers(settings, seed.admin)
    pid = await _create_project(client, owner)
    await client.post(
        f"/api/v1/envsync/projects/{pid}/env",
        json={"content": "SECRET=1\n", "base_version": None},
        headers=owner,
    )

    outsider = auth_headers(settings, seed.outsider)
    # 404 (not 403) so we don't even confirm the project exists to a non-member.
    env = await client.get(f"/api/v1/envsync/projects/{pid}/env", headers=outsider)
    assert env.status_code == 404
    assert (
        await client.get(f"/api/v1/envsync/projects/{pid}/collaborators", headers=outsider)
    ).status_code == 404
    push = await client.post(
        f"/api/v1/envsync/projects/{pid}/env",
        json={"content": "X=1\n", "base_version": None},
        headers=outsider,
    )
    assert push.status_code == 404
    # And the project never appears in their list.
    assert (await client.get("/api/v1/envsync/projects", headers=outsider)).json() == []


async def test_viewer_can_read_but_not_push(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    owner = auth_headers(settings, seed.admin)
    pid = await _create_project(client, owner)
    await client.post(
        f"/api/v1/envsync/projects/{pid}/env",
        json={"content": "A=1\n", "base_version": None},
        headers=owner,
    )
    # Add the report as a viewer.
    add = await client.post(
        f"/api/v1/envsync/projects/{pid}/collaborators",
        json={"email": seed.report.work_email, "role": "viewer"},
        headers=owner,
    )
    assert add.status_code == 201, add.text

    viewer = auth_headers(settings, seed.report)
    read = await client.get(f"/api/v1/envsync/projects/{pid}/env", headers=viewer)
    assert read.status_code == 200
    push = await client.post(
        f"/api/v1/envsync/projects/{pid}/env",
        json={"content": "A=2\n", "base_version": None},
        headers=viewer,
    )
    assert push.status_code == 403  # read-only

    # A non-owner cannot manage collaborators either.
    assert (
        await client.post(
            f"/api/v1/envsync/projects/{pid}/collaborators",
            json={"email": seed.outsider.work_email, "role": "editor"},
            headers=viewer,
        )
    ).status_code == 403

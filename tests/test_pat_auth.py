"""Personal Access Token: resolution rules and the mint/list/revoke endpoints.

Every protected endpoint gets an authorization test (Testing §9). The PAT is the
credential the Avora MCP server presents on behalf of an employee, so its
resolution rules (revoked / expired / inactive / unknown) are security-critical.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import generate_device_token, hash_device_token
from app.models import Employee, EmployeeStatus, Role
from app.repositories.audit import AuditRepository
from app.repositories.personal_access_token import PersonalAccessTokenRepository
from app.services.pat_service import PatService
from tests.conftest import _Seed, auth_headers


def _pat_service(db: AsyncSession, settings: Settings) -> PatService:
    return PatService(PersonalAccessTokenRepository(db), AuditRepository(db), settings)


async def _mint_raw(
    db: AsyncSession,
    settings: Settings,
    employee: Employee,
    *,
    expires_at: datetime | None = None,
    revoked: bool = False,
) -> str:
    raw = generate_device_token()
    repo = PersonalAccessTokenRepository(db)
    token = await repo.create_token(
        employee_id=employee.id,
        label="test",
        token_hash=hash_device_token(settings, raw),
        expires_at=expires_at,
    )
    if revoked:
        await repo.revoke_token(token)
    await db.commit()
    return raw


@pytest.mark.asyncio
async def test_valid_token_resolves_to_employee(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    raw = await _mint_raw(db, settings, seed.report)
    resolved = await _pat_service(db, settings).resolve_token(raw)
    assert resolved == seed.report.id


@pytest.mark.asyncio
async def test_touch_updates_last_used(db: AsyncSession, settings: Settings, seed: _Seed) -> None:
    raw = await _mint_raw(db, settings, seed.report)
    repo = PersonalAccessTokenRepository(db)
    before = (await repo.list_tokens(seed.report.id))[0].last_used_at
    assert before is None
    await _pat_service(db, settings).resolve_token(raw)
    after = (await repo.list_tokens(seed.report.id))[0].last_used_at
    assert after is not None


@pytest.mark.asyncio
async def test_revoked_token_does_not_resolve(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    raw = await _mint_raw(db, settings, seed.report, revoked=True)
    assert await _pat_service(db, settings).resolve_token(raw) is None


@pytest.mark.asyncio
async def test_expired_token_does_not_resolve(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    past = datetime.now(UTC) - timedelta(minutes=1)
    raw = await _mint_raw(db, settings, seed.report, expires_at=past)
    assert await _pat_service(db, settings).resolve_token(raw) is None


@pytest.mark.asyncio
async def test_future_expiry_still_resolves(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    future = datetime.now(UTC) + timedelta(days=30)
    raw = await _mint_raw(db, settings, seed.report, expires_at=future)
    assert await _pat_service(db, settings).resolve_token(raw) == seed.report.id


@pytest.mark.asyncio
async def test_unknown_token_does_not_resolve(db: AsyncSession, settings: Settings) -> None:
    assert await _pat_service(db, settings).resolve_token("not-a-real-token") is None


@pytest.mark.asyncio
async def test_mint_returns_raw_token_once_then_lists_without_it(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await client.post(
        "/api/v1/pat/tokens",
        json={"label": "MacBook - Claude Code"},
        headers=auth_headers(settings, seed.report),
    )
    assert res.status_code == 201
    body = res.json()
    assert body["token"]  # raw token present exactly once, at creation
    assert body["label"] == "MacBook - Claude Code"

    listed = await client.get("/api/v1/pat/tokens", headers=auth_headers(settings, seed.report))
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert "token" not in rows[0]  # never returned again


@pytest.mark.asyncio
async def test_revoke_own_token(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    created = await client.post(
        "/api/v1/pat/tokens",
        json={"label": "laptop"},
        headers=auth_headers(settings, seed.report),
    )
    token_id = created.json()["id"]
    res = await client.delete(
        f"/api/v1/pat/tokens/{token_id}", headers=auth_headers(settings, seed.report)
    )
    assert res.status_code == 204
    rows = (
        await client.get("/api/v1/pat/tokens", headers=auth_headers(settings, seed.report))
    ).json()
    assert rows[0]["is_revoked"] is True


@pytest.mark.asyncio
async def test_cannot_revoke_another_users_token(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await client.post(
        "/api/v1/pat/tokens",
        json={"label": "reports-token"},
        headers=auth_headers(settings, seed.report),
    )
    token_id = created.json()["id"]
    # The outsider must not be able to revoke a token they do not own (scoped 404).
    res = await client.delete(
        f"/api/v1/pat/tokens/{token_id}", headers=auth_headers(settings, seed.outsider)
    )
    assert res.status_code == 404
    # And it never appears in their own list.
    rows = (
        await client.get("/api/v1/pat/tokens", headers=auth_headers(settings, seed.outsider))
    ).json()
    assert rows == []


@pytest.mark.asyncio
async def test_mint_requires_authentication(client: AsyncClient) -> None:
    res = await client.post("/api/v1/pat/tokens", json={"label": "x"})
    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_inactive_employee_lookup(db: AsyncSession, settings: Settings, seed: _Seed) -> None:
    # A token for an employee who is later deactivated still resolves to their id
    # (resolution is identity-only); the MCP auth layer is what rejects inactive
    # accounts. Prove the token row itself is unaffected by is_active.
    inactive = Employee(
        hr_external_id="hr-inactive",
        work_email="inactive@corp.test",
        full_name="Ina Inactive",
        role=Role.EMPLOYEE,
        status=EmployeeStatus.INACTIVE,
        is_active=False,
    )
    db.add(inactive)
    await db.flush()
    raw = await _mint_raw(db, settings, inactive)
    assert await _pat_service(db, settings).resolve_token(raw) == inactive.id

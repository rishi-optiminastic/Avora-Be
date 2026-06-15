"""HR webhook: unsigned rejection + cannot set privilege (Security rule 5.5)."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Employee, Role
from tests.conftest import _Seed, hr_headers


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "hr_external_id": "hr-new-1",
        "work_email": "newhire@corp.io",
        "full_name": "Nina Newhire",
        "department": "Engineering",
        "manager_external_id": None,
        "status": "active",
        "start_date": None,
    }
    body.update(overrides)
    return body


async def test_unsigned_webhook_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.post("/api/v1/hr/sync", json=_payload())
    assert resp.status_code == 401


async def test_bad_signature_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    raw, headers = hr_headers(settings, _payload())
    headers["X-HR-Signature"] = "sha256=00000000"
    resp = await client.post("/api/v1/hr/sync", content=raw, headers=headers)
    assert resp.status_code == 401


async def test_valid_webhook_creates_employee(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    raw, headers = hr_headers(settings, _payload())
    resp = await client.post("/api/v1/hr/sync", content=raw, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["hr_external_id"] == "hr-new-1"
    # HR-created employees default to the least-privileged role.
    assert body["role"] == "employee"


async def test_webhook_cannot_set_role(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    # Even if HR smuggles a `role` field, the strict schema ignores it and the
    # employee is created as a plain employee — privilege never escalates.
    raw, headers = hr_headers(settings, _payload(role="admin"))
    resp = await client.post("/api/v1/hr/sync", content=raw, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["role"] == "employee"

    created = await db.scalar(select(Employee).where(Employee.hr_external_id == "hr-new-1"))
    assert created is not None
    assert created.role is Role.EMPLOYEE


async def test_webhook_offboard_soft_deletes(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    raw, headers = hr_headers(
        settings,
        _payload(hr_external_id="hr-report", work_email="report@corp.io", status="inactive"),
    )
    resp = await client.post("/api/v1/hr/sync", content=raw, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_active"] is False
    assert body["status"] == "inactive"

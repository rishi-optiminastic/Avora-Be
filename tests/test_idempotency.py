"""Generic idempotency store (IdempotencyService) — replay protection.

Two layers of proof:
- Service unit tests: the wrapped operation runs AT MOST ONCE per key (this is
  what stops a replayed `/tasks/parse` from paying for the LLM twice), and a key
  reused with a different body is rejected.
- API tests through `POST /tasks`: a replayed create returns the original row
  (one task, not two), keyless calls are unprotected (back-compat), and one
  user's key never collides with another's.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.repositories.idempotency import IdempotencyRepository
from app.services.idempotency import IdempotencyService
from tests.conftest import _Seed, auth_headers


# --------------------------------------------------------------------------- #
# Service layer — the operation runs once; replay returns the stored result.
# --------------------------------------------------------------------------- #
async def test_run_executes_operation_once_on_replay(db: AsyncSession) -> None:
    service = IdempotencyService(IdempotencyRepository(db))
    principal = uuid.uuid4()
    calls = 0

    async def op() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": calls}

    first = await service.run(
        principal_id=principal, scope="t.op", key="k1", request={"a": 1}, operation=op
    )
    second = await service.run(
        principal_id=principal, scope="t.op", key="k1", request={"a": 1}, operation=op
    )

    assert calls == 1  # the expensive work happened exactly once
    assert first == {"value": 1}
    assert second == {"value": 1}  # replay returned the stored response


async def test_run_without_key_executes_every_time(db: AsyncSession) -> None:
    service = IdempotencyService(IdempotencyRepository(db))
    calls = 0

    async def op() -> int:
        nonlocal calls
        calls += 1
        return calls

    await service.run(principal_id=uuid.uuid4(), scope="t.op", key=None, request={}, operation=op)
    await service.run(principal_id=uuid.uuid4(), scope="t.op", key=None, request={}, operation=op)
    assert calls == 2  # no key => no protection (existing clients unaffected)


async def test_run_rejects_same_key_with_different_body(db: AsyncSession) -> None:
    service = IdempotencyService(IdempotencyRepository(db))
    principal = uuid.uuid4()

    async def op() -> str:
        return "ok"

    await service.run(
        principal_id=principal, scope="t.op", key="k2", request={"a": 1}, operation=op
    )
    with pytest.raises(ValidationError):
        await service.run(
            principal_id=principal, scope="t.op", key="k2", request={"a": 2}, operation=op
        )


async def test_same_key_different_scope_does_not_collide(db: AsyncSession) -> None:
    service = IdempotencyService(IdempotencyRepository(db))
    principal = uuid.uuid4()
    seen: list[str] = []

    async def op_a() -> str:
        seen.append("a")
        return "a"

    async def op_b() -> str:
        seen.append("b")
        return "b"

    a = await service.run(
        principal_id=principal, scope="scope.a", key="same", request={}, operation=op_a
    )
    b = await service.run(
        principal_id=principal, scope="scope.b", key="same", request={}, operation=op_b
    )
    assert (a, b) == ("a", "b")  # same key, different operation => both run
    assert seen == ["a", "b"]


# --------------------------------------------------------------------------- #
# API layer — through POST /tasks (manager creating for a report).
# --------------------------------------------------------------------------- #
def _task_body(seed: _Seed) -> dict[str, str]:
    return {"title": "Ship it", "assignee_id": str(seed.report.id), "cadence": "daily"}


async def test_replayed_create_returns_one_task(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    headers = {**auth_headers(settings, seed.manager), "Idempotency-Key": "create-key-1"}
    body = _task_body(seed)

    first = await client.post("/api/v1/tasks", json=body, headers=headers)
    assert first.status_code == 201, first.text
    second = await client.post("/api/v1/tasks", json=body, headers=headers)
    assert second.status_code == 201, second.text

    assert first.json()["id"] == second.json()["id"]  # same row, not a duplicate

    listed = await client.get(
        f"/api/v1/tasks?assignee_id={seed.report.id}",
        headers=auth_headers(settings, seed.manager),
    )
    titles = [t for t in listed.json()["items"] if t["title"] == "Ship it"]
    assert len(titles) == 1  # exactly one task created despite two POSTs


async def test_create_without_key_is_not_deduped(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    headers = auth_headers(settings, seed.manager)
    body = _task_body(seed)
    first = await client.post("/api/v1/tasks", json=body, headers=headers)
    second = await client.post("/api/v1/tasks", json=body, headers=headers)
    assert first.json()["id"] != second.json()["id"]  # two distinct tasks


async def test_same_key_different_body_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    headers = {**auth_headers(settings, seed.manager), "Idempotency-Key": "create-key-2"}
    await client.post("/api/v1/tasks", json=_task_body(seed), headers=headers)
    changed = await client.post(
        "/api/v1/tasks",
        json={**_task_body(seed), "title": "Different title"},
        headers=headers,
    )
    assert changed.status_code == 422, changed.text


async def test_one_users_key_does_not_collide_with_another(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = _task_body(seed)
    manager_resp = await client.post(
        "/api/v1/tasks",
        json=body,
        headers={**auth_headers(settings, seed.manager), "Idempotency-Key": "shared"},
    )
    admin_resp = await client.post(
        "/api/v1/tasks",
        json=body,
        headers={**auth_headers(settings, seed.admin), "Idempotency-Key": "shared"},
    )
    assert manager_resp.status_code == 201
    assert admin_resp.status_code == 201
    # Same key string, different callers => the keys are namespaced, so two rows.
    assert manager_resp.json()["id"] != admin_resp.json()["id"]

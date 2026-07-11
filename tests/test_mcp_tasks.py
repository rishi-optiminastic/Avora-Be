"""Avora MCP task tools.

The tools are thin adapters over TaskService/EmployeeService, so these tests
focus on the new surface: PAT authentication inside a tool, and that the tools
inherit the services' scope/authorization (a non-manager cannot assign to someone
else). The tools build their own session and services, so we point their module
globals at the test database, settings, and a no-op email (Testing §9: no
network).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import generate_device_token, hash_device_token
from app.mcp import server
from app.models import Employee
from app.repositories.personal_access_token import PersonalAccessTokenRepository
from tests.conftest import _Seed


class _FakeEmail:
    """No-op email so a manager assignment never hits SendGrid in tests."""

    def __init__(self, settings: Settings) -> None:
        pass

    async def send_task_assigned(self, **kwargs: object) -> None:
        return None


def _ctx(raw: str | None) -> object:
    headers = {"authorization": f"Bearer {raw}"} if raw is not None else {}
    request = type("Req", (), {"headers": headers})()
    request_context = type("Rc", (), {"request": request})()
    return type("Ctx", (), {"request_context": request_context})()


@pytest.fixture
def mcp_env(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Point the MCP tools' module globals at the test DB, settings, and a no-op
    email service (they construct their own session/services, outside FastAPI DI)."""
    monkeypatch.setattr(server, "SessionFactory", session_factory)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "EmailService", _FakeEmail)


async def _mint(
    db: AsyncSession, settings: Settings, employee: Employee, *, revoked: bool = False
) -> str:
    raw = generate_device_token()
    repo = PersonalAccessTokenRepository(db)
    token = await repo.create_token(
        employee_id=employee.id,
        label="mcp",
        token_hash=hash_device_token(settings, raw),
        expires_at=None,
    )
    if revoked:
        await repo.revoke_token(token)
    await db.commit()
    return raw


@pytest.mark.asyncio
async def test_whoami_returns_caller(
    mcp_env: None, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    raw = await _mint(db, settings, seed.report)
    result = json.loads(await server.whoami(_ctx(raw)))
    assert result["id"] == str(seed.report.id)
    assert result["email"] == seed.report.work_email


@pytest.mark.asyncio
async def test_missing_and_revoked_tokens_are_rejected(
    mcp_env: None, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    with pytest.raises(ToolError):
        await server.whoami(_ctx(None))
    revoked = await _mint(db, settings, seed.report, revoked=True)
    with pytest.raises(ToolError):
        await server.whoami(_ctx(revoked))
    with pytest.raises(ToolError):
        await server.whoami(_ctx("garbage-token"))


@pytest.mark.asyncio
async def test_self_assign_create_and_complete(
    mcp_env: None, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    raw = await _mint(db, settings, seed.report)
    created = json.loads(
        await server.create_task(_ctx(raw), title="Write the weekly report")
    )
    assert created["assignee_id"] == str(seed.report.id)
    assert created["status"] == "todo"

    completed = json.loads(await server.complete_task(_ctx(raw), task_id=created["id"]))
    assert completed["status"] == "done"
    assert completed["completed_at"] is not None


@pytest.mark.asyncio
async def test_non_manager_cannot_assign_to_others(
    mcp_env: None, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    raw = await _mint(db, settings, seed.report)
    with pytest.raises(ToolError):
        await server.create_task(
            _ctx(raw), title="Do my chore", assignee_id=str(seed.outsider.id)
        )


@pytest.mark.asyncio
async def test_manager_can_assign_to_report(
    mcp_env: None, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    raw = await _mint(db, settings, seed.manager)
    created = json.loads(
        await server.create_task(
            _ctx(raw),
            title="Ship the feature",
            assignee_id=str(seed.report.id),
            description="Full context here",
            priority="high",
        )
    )
    assert created["assignee_id"] == str(seed.report.id)
    assert created["assigned_by_id"] == str(seed.manager.id)
    assert created["priority"] == "high"


@pytest.mark.asyncio
async def test_list_my_tasks_is_scoped(
    mcp_env: None, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    report_raw = await _mint(db, settings, seed.report)
    manager_raw = await _mint(db, settings, seed.manager)
    await server.create_task(_ctx(report_raw), title="Report task A")
    await server.create_task(_ctx(manager_raw), title="Manager task B")

    mine = json.loads(await server.list_my_tasks(_ctx(report_raw)))
    titles = {t["title"] for t in mine["tasks"]}
    assert titles == {"Report task A"}


@pytest.mark.asyncio
async def test_blocked_requires_reason(
    mcp_env: None, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    raw = await _mint(db, settings, seed.report)
    created = json.loads(await server.create_task(_ctx(raw), title="A task"))
    with pytest.raises(ToolError):
        await server.update_task(_ctx(raw), task_id=created["id"], status="blocked")
    # With a reason it succeeds.
    updated = json.loads(
        await server.update_task(
            _ctx(raw), task_id=created["id"], status="blocked", blocked_reason="waiting on API"
        )
    )
    assert updated["status"] == "blocked"
    assert updated["blocked_reason"] == "waiting on API"


@pytest.mark.asyncio
async def test_add_comment(
    mcp_env: None, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    raw = await _mint(db, settings, seed.report)
    created = json.loads(await server.create_task(_ctx(raw), title="Commentable"))
    comment = json.loads(
        await server.add_comment(_ctx(raw), task_id=created["id"], body="Done the first half")
    )
    assert comment["task_id"] == created["id"]
    assert comment["body"] == "Done the first half"


@pytest.mark.asyncio
async def test_get_task_returns_full_detail(
    mcp_env: None, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    raw = await _mint(db, settings, seed.report)
    created = json.loads(
        await server.create_task(
            _ctx(raw), title="Detailed", description="the full brief", due_date=_tomorrow()
        )
    )
    fetched = json.loads(await server.get_task(_ctx(raw), task_id=created["id"]))
    assert fetched["description"] == "the full brief"
    assert fetched["title"] == "Detailed"


def _tomorrow() -> str:
    return (datetime.now(UTC) + timedelta(days=1)).date().isoformat()

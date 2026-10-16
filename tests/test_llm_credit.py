"""LLM API credit card: admin-only access, defensive parsing of OpenRouter's
replies, the cache, and the one-time low-balance alert. The provider is always a
`httpx.MockTransport` - no test reaches OpenRouter (Testing §9)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.notification import Notification
from app.repositories.employee import EmployeeRepository
from app.repositories.notification import NotificationRepository
from app.schemas.auth import CurrentUser
from app.services import llm_credit_service
from app.services.llm_credit_service import CreditCache, LlmCreditService
from app.services.notification_service import NotificationService
from tests.conftest import _Seed, auth_headers

_KEY = "sk-or-test-SECRET-key"
Handler = Callable[[httpx.Request], httpx.Response]


class _FakeOpenRouter:
    """Serves canned /key and /credits replies and records every request."""

    def __init__(self, key: Handler, credits: Handler | None = None) -> None:
        self._key = key
        self._credits = credits or (lambda _r: httpx.Response(403, json={"error": "mgmt only"}))
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/key"):
            return self._key(request)
        return self._credits(request)


def _key_reply(**data: object) -> Handler:
    return lambda _r: httpx.Response(200, json={"data": data})


def _service(
    db: AsyncSession, settings: Settings, fake: _FakeOpenRouter, cache: CreditCache
) -> LlmCreditService:
    return LlmCreditService(
        settings.model_copy(update={"openrouter_api_key": _KEY}),
        EmployeeRepository(db),
        NotificationService(NotificationRepository(db)),
        cache=cache,
        transport=httpx.MockTransport(fake.handler),
    )


def _admin(seed: _Seed) -> CurrentUser:
    return CurrentUser(employee_id=seed.admin.id, role=Role.ADMIN, manager_id=None)


async def _second_admin(db: AsyncSession) -> Employee:
    it_admin = Employee(
        hr_external_id="hr-it",
        work_email="it@corp.test",
        full_name="Ivy IT",
        role=Role.IT_ADMIN,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(it_admin)
    await db.commit()
    return it_admin


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process cache must not carry one test's answer into the next."""
    monkeypatch.setattr(llm_credit_service, "_CACHE", CreditCache())


async def test_endpoint_is_admin_only(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    hr = Employee(
        hr_external_id="hr-hr",
        work_email="hr@corp.test",
        full_name="Hana HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    it_admin = await _second_admin(db)

    # Everyone but an admin gets 404 - not even the existence of a paid key leaks.
    for caller in (seed.report, seed.manager, seed.outsider, hr):
        res = await client.get("/api/v1/llm-credit", headers=auth_headers(settings, caller))
        assert res.status_code == 404

    # Admins (an IT admin counts) get an answer; with no key configured in tests
    # it is "unconfigured", never an error and never a provider call.
    for caller in (seed.admin, it_admin):
        res = await client.get("/api/v1/llm-credit", headers=auth_headers(settings, caller))
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "unconfigured"
        assert body["remaining"] is None and body["low"] is False
        assert body["currency"] == "USD"


async def test_key_cap_is_reported_and_flagged_low(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    fake = _FakeOpenRouter(_key_reply(usage=6.8, limit=10.0, limit_remaining=3.2))
    credit = await _service(db, settings, fake, CreditCache()).credit_for(_admin(seed))

    assert credit.status == "ok"
    assert credit.remaining == 3.2 and credit.usage == 6.8 and credit.limit == 10.0
    assert credit.low is True  # under the default 5 USD threshold
    # The key travels only in the Authorization header, and never comes back.
    assert all(r.headers["authorization"] == f"Bearer {_KEY}" for r in fake.requests)
    assert _KEY not in credit.model_dump_json()


async def test_uncapped_key_falls_back_to_the_account_balance(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    fake = _FakeOpenRouter(
        _key_reply(usage=20.0, limit=None, limit_remaining=None, is_free_tier=False),
        credits=lambda _r: httpx.Response(
            200, json={"data": {"total_credits": 50.0, "total_usage": 20.0}}
        ),
    )
    credit = await _service(db, settings, fake, CreditCache()).credit_for(_admin(seed))

    assert credit.status == "ok"
    assert credit.remaining == 30.0 and credit.limit == 50.0 and credit.low is False


async def test_effective_budget_counts_when_the_key_has_no_cap(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    fake = _FakeOpenRouter(
        _key_reply(
            usage=1.0,
            limit=None,
            limit_remaining=None,
            effective_budget={"limit_usd": 25.0, "spend_usd": 1.0, "remaining_usd": 24.0},
        )
    )
    credit = await _service(db, settings, fake, CreditCache()).credit_for(_admin(seed))
    assert credit.remaining == 24.0 and credit.limit == 25.0


@pytest.mark.parametrize(
    "key_handler",
    [
        pytest.param(lambda _r: httpx.Response(500, text="boom"), id="server-error"),
        pytest.param(lambda _r: httpx.Response(401, json={"error": "bad key"}), id="bad-key"),
        pytest.param(lambda _r: httpx.Response(200, text="<html>not json"), id="not-json"),
        pytest.param(lambda _r: httpx.Response(200, json={"data": "nope"}), id="wrong-shape"),
    ],
)
async def test_a_bad_provider_reply_reads_as_unknown(
    db: AsyncSession, seed: _Seed, settings: Settings, key_handler: Handler
) -> None:
    fake = _FakeOpenRouter(key_handler)
    credit = await _service(db, settings, fake, CreditCache()).credit_for(_admin(seed))
    assert credit.status == "unknown" and credit.remaining is None and credit.low is False


async def test_an_unreachable_provider_reads_as_unknown(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    credit = await _service(db, settings, _FakeOpenRouter(down), CreditCache()).credit_for(
        _admin(seed)
    )
    assert credit.status == "unknown"


async def test_answers_are_cached_and_refresh_is_throttled(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    fake = _FakeOpenRouter(_key_reply(usage=1.0, limit=100.0, limit_remaining=99.0))
    cache = CreditCache()
    service = _service(db, settings, fake, cache)

    first = await service.credit_for(_admin(seed))
    calls = len(fake.requests)
    # A second read and an immediate refresh both reuse the snapshot.
    assert await service.credit_for(_admin(seed)) == first
    assert await service.credit_for(_admin(seed), refresh=True) == first
    assert len(fake.requests) == calls

    # Once the snapshot is older than the refresh floor, refresh asks again.
    cache.fetched_at -= 31
    await service.credit_for(_admin(seed), refresh=True)
    assert len(fake.requests) > calls


async def test_admins_are_alerted_once_when_the_balance_turns_low(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    it_admin = await _second_admin(db)
    fake = _FakeOpenRouter(_key_reply(usage=9.0, limit=10.0, limit_remaining=1.0))
    cache = CreditCache()
    service = _service(db, settings, fake, cache)

    await service.credit_for(_admin(seed))
    cache.fetched_at -= 601  # expire, still low: no second alert
    await service.credit_for(_admin(seed))

    rows = (await db.execute(select(Notification))).scalars().all()
    # The other admin hears about it exactly once; the admin who is looking at
    # the card is not notified about their own page view.
    assert [n.recipient_id for n in rows] == [it_admin.id]
    assert rows[0].entity_type == "llm_credit"
    assert _KEY not in json.dumps([n.body for n in rows])

"""Circle data in Avora: compensation pre-fill and Circle documents.

The real CircleClient runs against an in-process fake of Circle (httpx mock
transport), so the secret header, the email lookup and error mapping are all
exercised. Authorization mirrors Avora's own pay/document rules.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.deps import get_circle_client, get_db, get_jwks_client, get_settings
from app.main import create_app
from app.models.audit import AuditLog
from app.services.circle_client import CircleClient
from tests.conftest import _FakeJwksClient, _Seed, auth_headers

CIRCLE_SECRET = "test-circle-avora-secret"

# The seed's employees, keyed by work email (see conftest.seed).
_CIRCLE_PEOPLE: dict[str, dict[str, Any]] = {
    "report@corp.test": {
        "compensation": {
            "employee_code": "EMP-1001",
            "annual_ctc_text": "6 LPA",
            "annual_ctc_inr": 600_000,
            "pf_enabled": False,
            "joining_date": "2026-08-21",
            "bank": {"bank_name": "HDFC", "account_number": "123456789", "ifsc_code": "bad-ifsc"},
        },
        "documents": [
            {
                "id": "c1a2b3c4d5e6",
                "file_name": "aadhaar.pdf",
                "category": "Aadhaar card",
                "content_type": "application/pdf",
                "size": 10,
                "uploaded_at": "2026-08-20",
            },
            {
                "id": "f1e2d3c4b5a6",
                "file_name": "notes.pdf",
                "category": "document",
                "content_type": "application/pdf",
                "size": 5,
                "uploaded_at": "2026-08-21",
            },
        ],
    },
}


class FakeCircle:
    def __init__(self) -> None:
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(500)
        if request.headers.get("X-Avora-Secret") != CIRCLE_SECRET:
            return httpx.Response(403)
        person = _CIRCLE_PEOPLE.get(request.url.params.get("email", ""))
        if person is None:
            return httpx.Response(404)
        path = request.url.path.removeprefix("/api/internal/avora")
        if path == "/compensation":
            return httpx.Response(200, json=person["compensation"])
        if path == "/documents":
            return httpx.Response(200, json={"documents": person["documents"]})
        doc_id = path.split("/")[2]
        if any(d["id"] == doc_id for d in person["documents"]):
            return httpx.Response(
                200, content=f"bytes:{doc_id}".encode(), headers={"content-type": "application/pdf"}
            )
        return httpx.Response(404)


@pytest_asyncio.fixture
async def circle_client(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[tuple[AsyncClient, FakeCircle]]:
    configured = settings.model_copy(
        update={"circle_api_url": "http://circle.invalid", "circle_api_secret": CIRCLE_SECRET}
    )
    fake = FakeCircle()
    app = create_app(configured)

    async def _db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s
            await s.commit()

    app.dependency_overrides[get_settings] = lambda: configured
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_jwks_client] = lambda: _FakeJwksClient()
    app.dependency_overrides[get_circle_client] = lambda: CircleClient(
        configured, transport=httpx.MockTransport(fake.handler)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, fake


def _prefill_url(seed: _Seed) -> str:
    return f"/api/v1/employees/{seed.report.id}/compensation/circle-prefill"


def _docs_url(seed: _Seed, suffix: str = "") -> str:
    return f"/api/v1/employees/{seed.report.id}/documents/circle{suffix}"


# --- compensation pre-fill ----------------------------------------------------------


async def test_admin_gets_prefill_shaped_for_the_forms(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, _ = circle_client
    resp = await client.get(_prefill_url(seed), headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["amount_minor"] == 60_000_000  # 6 LPA in paise
    assert body["currency"] == "INR" and body["period"] == "annual"
    assert body["pf_enabled"] is False
    assert body["effective_date"] == "2026-08-21"
    assert body["bank_name"] == "HDFC" and body["account_number"] == "123456789"
    # A value Avora would reject is dropped with a warning, not passed through.
    assert body["ifsc_code"] is None
    assert any("IFSC" in w for w in body["warnings"])


async def test_prefill_saves_nothing(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, _ = circle_client
    await client.get(_prefill_url(seed), headers=auth_headers(settings, seed.admin))
    resp = await client.get(
        f"/api/v1/employees/{seed.report.id}/compensation",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 404


async def test_prefill_is_hidden_from_the_person_and_their_manager(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, _ = circle_client
    for viewer in (seed.report, seed.manager, seed.outsider):
        resp = await client.get(_prefill_url(seed), headers=auth_headers(settings, viewer))
        assert resp.status_code == 403


async def test_prefill_for_someone_circle_does_not_know(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, _ = circle_client
    resp = await client.get(
        f"/api/v1/employees/{seed.outsider.id}/compensation/circle-prefill",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200 and resp.json()["found"] is False


async def test_prefill_reports_circle_outage(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, fake = circle_client
    fake.down = True
    resp = await client.get(_prefill_url(seed), headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 502


async def test_prefill_off_when_circle_not_configured(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get(_prefill_url(seed), headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is False and body["amount_minor"] is None


# --- documents ------------------------------------------------------------------------


async def test_person_and_admin_see_circle_documents(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, _ = circle_client
    for viewer in (seed.admin, seed.report):
        resp = await client.get(_docs_url(seed), headers=auth_headers(settings, viewer))
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert [(d["id"], d["category"]) for d in body["documents"]] == [
            ("c1a2b3c4d5e6", "identity"),
            ("f1e2d3c4b5a6", "other"),
        ]


async def test_manager_and_outsider_cannot_list(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, _ = circle_client
    for viewer in (seed.manager, seed.outsider):
        resp = await client.get(_docs_url(seed), headers=auth_headers(settings, viewer))
        assert resp.status_code == 403


async def test_download_streams_through_avora_and_is_audited(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    client, _ = circle_client
    resp = await client.get(
        _docs_url(seed, "/c1a2b3c4d5e6/download"), headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 200
    assert resp.content == b"bytes:c1a2b3c4d5e6"
    assert resp.headers["content-type"] == "application/octet-stream"
    assert 'filename="aadhaar.pdf"' in resp.headers["content-disposition"]
    actions = (await db.scalars(select(AuditLog.action))).all()
    assert "document.circle_download" in actions


async def test_download_out_of_scope_is_404(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, _ = circle_client
    resp = await client.get(
        _docs_url(seed, "/c1a2b3c4d5e6/download"), headers=auth_headers(settings, seed.outsider)
    )
    assert resp.status_code == 404


async def test_cannot_download_a_document_not_in_the_persons_list(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, _ = circle_client
    resp = await client.get(
        _docs_url(seed, "/0000deadbeef/download"), headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 404


async def test_documents_hidden_when_circle_not_configured(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get(_docs_url(seed), headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 200 and resp.json() == {"configured": False, "documents": []}


async def test_existing_document_list_is_unchanged(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, _ = circle_client
    resp = await client.get(
        f"/api/v1/employees/{seed.report.id}/documents", headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 200 and json.loads(resp.content) == []

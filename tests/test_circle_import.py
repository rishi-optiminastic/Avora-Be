"""Circle data in Avora: compensation pre-fill and copied Circle documents.

The real CircleClient runs against an in-process fake of Circle (httpx mock
transport), so the secret header, the email lookup and error mapping are all
exercised. Authorization mirrors Avora's own pay/document rules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.deps import get_circle_client, get_db, get_jwks_client, get_settings
from app.core.exceptions import UpstreamUnavailableError
from app.main import create_app
from app.models.audit import AuditLog
from app.models.document import EmployeeDocument
from app.models.employee import Employee, Gender
from app.repositories.audit import AuditRepository
from app.repositories.circle_document_import import CircleDocumentImportRepository
from app.repositories.document import DocumentRepository
from app.repositories.employee import EmployeeRepository
from app.services.circle_client import CircleClient
from app.services.circle_document_sync import CircleDocumentSync
from app.services.circle_profile_sync import CircleProfileSync, map_gender, parse_birth_date
from app.services.document_service import MAX_DOC_BYTES
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
        self.garbled = False
        self.broken_ids: set[str] = set()  # content fetch fails
        self.empty_ids: set[str] = set()  # content is empty
        self.huge_ids: set[str] = set()  # content is bigger than Avora allows
        self.extra_docs: list[dict[str, Any]] = []  # listed but not downloadable
        self.compensation: dict[str, Any] | None = None  # override the reply
        self.profile: dict[str, Any] = {
            "date_of_birth": "1998-04-12",
            "gender": "Female",
            "avatar_document_id": "ava000000001",
        }
        self.avatar: bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        self.avatar_fetches = 0
        self.content_fetches = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(500)
        if self.garbled:
            return httpx.Response(200, content=b"<html>proxy error</html>")
        if request.headers.get("X-Avora-Secret") != CIRCLE_SECRET:
            return httpx.Response(403)
        person = _CIRCLE_PEOPLE.get(request.url.params.get("email", ""))
        if person is None:
            return httpx.Response(404)
        path = request.url.path.removeprefix("/api/internal/avora")
        if path == "/profile":
            return httpx.Response(200, json=self.profile)
        if path == "/avatar":
            self.avatar_fetches += 1
            return httpx.Response(200, content=self.avatar, headers={"content-type": "image/png"})
        if path == "/compensation":
            return httpx.Response(200, json=self.compensation or person["compensation"])
        if path == "/documents":
            return httpx.Response(200, json={"documents": person["documents"] + self.extra_docs})
        doc_id = path.split("/")[2]
        self.content_fetches += 1
        if doc_id in self.broken_ids:
            return httpx.Response(500)
        if doc_id in self.empty_ids:
            return httpx.Response(200, content=b"", headers={"content-type": "application/pdf"})
        if doc_id in self.huge_ids:
            return httpx.Response(200, content=b"x" * (MAX_DOC_BYTES + 1))
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
    assert body["configured"] is False


async def test_garbled_circle_reply_is_unavailable_not_a_crash(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, fake = circle_client
    fake.garbled = True
    resp = await client.get(_prefill_url(seed), headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 502


# --- documents: copied into Avora, once per file -------------------------------------


@pytest.fixture
def scheduler(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, FakeCircle]:
    """The real scheduler module, wired to the test database and a fake Circle."""
    from worker import circle_documents_scheduler as module

    fake = FakeCircle()
    configured = settings.model_copy(
        update={"circle_api_url": "http://circle.invalid", "circle_api_secret": CIRCLE_SECRET}
    )

    def build(session: AsyncSession) -> CircleDocumentSync:
        return CircleDocumentSync(
            DocumentRepository(session),
            CircleDocumentImportRepository(session),
            AuditRepository(session),
            configured,
            CircleClient(configured, transport=httpx.MockTransport(fake.handler)),
        )

    def build_profile(session: AsyncSession) -> CircleProfileSync:
        return CircleProfileSync(
            EmployeeRepository(session),
            CircleDocumentImportRepository(session),
            AuditRepository(session),
            configured,
            CircleClient(configured, transport=httpx.MockTransport(fake.handler)),
        )

    monkeypatch.setattr(module, "SessionFactory", session_factory)
    monkeypatch.setattr(module, "_build_service", build)
    monkeypatch.setattr(module, "_build_profile_sync", build_profile)
    return module, fake


async def _copy_report(scheduler: tuple[Any, FakeCircle], seed: _Seed) -> int:
    module, _ = scheduler
    copied: int = await module.copy_for_employee(seed.report.id, "report@corp.test")
    return copied


async def _avora_docs(db: AsyncSession, seed: _Seed) -> list[EmployeeDocument]:
    # The scheduler writes through its own sessions; reload rather than trust
    # this session's identity map.
    rows = await db.scalars(
        select(EmployeeDocument)
        .where(EmployeeDocument.employee_id == seed.report.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def test_copies_each_circle_document_into_avora(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    assert await _copy_report(scheduler, seed) == 2
    docs = {d.original_filename: d for d in await _avora_docs(db, seed)}
    assert docs["aadhaar.pdf"].title == "Aadhaar card"
    assert docs["aadhaar.pdf"].category.value == "identity"
    assert docs["aadhaar.pdf"].content == b"bytes:c1a2b3c4d5e6"
    assert docs["aadhaar.pdf"].uploaded_by is None
    # Circle's generic "document" label falls back to the file name.
    assert docs["notes.pdf"].title == "notes.pdf"
    actions = (await db.scalars(select(AuditLog.action))).all()
    assert actions.count("document.circle_copy") == 2


async def test_running_again_copies_nothing_twice(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    await _copy_report(scheduler, seed)
    assert await _copy_report(scheduler, seed) == 0
    assert len(await _avora_docs(db, seed)) == 2


async def test_a_copy_deleted_in_avora_is_not_brought_back(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    await _copy_report(scheduler, seed)
    first = (await _avora_docs(db, seed))[0]
    await db.delete(first)
    await db.commit()
    assert await _copy_report(scheduler, seed) == 0
    assert len(await _avora_docs(db, seed)) == 1


async def test_one_broken_file_does_not_block_the_others(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    _, fake = scheduler
    fake.broken_ids = {"c1a2b3c4d5e6"}
    assert await _copy_report(scheduler, seed) == 1
    assert [d.original_filename for d in await _avora_docs(db, seed)] == ["notes.pdf"]
    # Once Circle serves it again, the next run picks it up.
    fake.broken_ids = set()
    assert await _copy_report(scheduler, seed) == 1
    assert len(await _avora_docs(db, seed)) == 2


async def test_uncopyable_file_is_recorded_and_not_fetched_every_run(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    _, fake = scheduler
    fake.empty_ids = {"f1e2d3c4b5a6"}
    assert await _copy_report(scheduler, seed) == 1
    fetches = fake.content_fetches
    assert await _copy_report(scheduler, seed) == 0
    assert fake.content_fetches == fetches  # nothing downloaded again
    actions = (await db.scalars(select(AuditLog.action))).all()
    assert "document.circle_skip" in actions


async def test_stored_bytes_are_removed_when_saving_fails(
    seed: _Seed, settings: Settings, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import circle_document_sync as module

    deleted: list[list[str]] = []

    async def fake_store(*_: Any, **__: Any) -> tuple[str, str, None]:
        return "application/pdf", "Avora/workspace-files/orphan.pdf", None

    async def fake_delete(keys: list[str]) -> None:
        deleted.append(keys)

    async def failing_add_file(*_: Any, **__: Any) -> None:
        raise RuntimeError("db write failed")

    monkeypatch.setattr(module, "store_document_bytes", fake_store)
    monkeypatch.setattr(module.storage, "delete_objects", fake_delete)
    configured = settings.model_copy(
        update={"circle_api_url": "http://circle.invalid", "circle_api_secret": CIRCLE_SECRET}
    )
    documents = DocumentRepository(db)
    monkeypatch.setattr(documents, "add_file", failing_add_file)
    sync = CircleDocumentSync(
        documents,
        CircleDocumentImportRepository(db),
        AuditRepository(db),
        configured,
        CircleClient(configured, transport=httpx.MockTransport(FakeCircle().handler)),
    )
    entry = _CIRCLE_PEOPLE["report@corp.test"]["documents"][0]
    with pytest.raises(RuntimeError):
        await sync.copy_one(seed.report.id, "report@corp.test", entry)
    assert deleted == [["Avora/workspace-files/orphan.pdf"]]


async def test_someone_circle_does_not_know_has_nothing_to_copy(
    scheduler: tuple[Any, FakeCircle], seed: _Seed
) -> None:
    module, _ = scheduler
    assert await module.copy_for_employee(seed.outsider.id, "outsider@corp.test") == 0


async def test_circle_outage_copies_nothing(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    module, fake = scheduler
    fake.down = True
    with pytest.raises(UpstreamUnavailableError):
        await module.copy_for_employee(seed.report.id, "report@corp.test")
    assert await _avora_docs(db, seed) == []
    assert await module.run_once() == 0  # the pass itself survives the outage


async def test_full_pass_copies_for_everyone_circle_knows(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    module, _ = scheduler
    assert await module.run_once() == 2
    assert await module.run_once() == 0


async def test_copies_follow_avoras_document_rules(
    scheduler: tuple[Any, FakeCircle],
    client: AsyncClient,
    settings: Settings,
    seed: _Seed,
    db: AsyncSession,
) -> None:
    await _copy_report(scheduler, seed)
    url = f"/api/v1/employees/{seed.report.id}/documents"
    for viewer in (seed.admin, seed.report):
        resp = await client.get(url, headers=auth_headers(settings, viewer))
        assert resp.status_code == 200 and len(resp.json()) == 2
    for viewer in (seed.manager, seed.outsider):
        resp = await client.get(url, headers=auth_headers(settings, viewer))
        assert resp.status_code == 403
    doc_id = (await _avora_docs(db, seed))[0].id
    download = await client.get(
        f"{url}/{doc_id}/download", headers=auth_headers(settings, seed.report)
    )
    assert download.status_code == 200 and download.content.startswith(b"bytes:")


async def test_odd_but_valid_json_from_circle_becomes_warnings_not_500(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, fake = circle_client
    fake.compensation = {
        "employee_code": 1042,
        "annual_ctc_text": None,
        "annual_ctc_inr": 1_200_000.0,
        "pf_enabled": "Y",
        "joining_date": "soon",
        "bank": "not-an-object",
    }
    resp = await client.get(_prefill_url(seed), headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["employee_code"] == "1042"
    assert body["amount_minor"] == 120_000_000  # a float CTC is still read
    assert body["pf_enabled"] is None and body["effective_date"] is None


async def test_boolean_ctc_is_not_read_as_money(
    circle_client: tuple[AsyncClient, FakeCircle], settings: Settings, seed: _Seed
) -> None:
    client, fake = circle_client
    fake.compensation = {"annual_ctc_inr": True, "annual_ctc_text": "true"}
    body = (await client.get(_prefill_url(seed), headers=auth_headers(settings, seed.admin))).json()
    assert body["amount_minor"] is None and body["warnings"]


async def test_oversized_file_is_skipped_without_retrying(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    _, fake = scheduler
    fake.huge_ids = {"c1a2b3c4d5e6"}
    assert await _copy_report(scheduler, seed) == 1
    fetches = fake.content_fetches
    assert await _copy_report(scheduler, seed) == 0
    assert fake.content_fetches == fetches


async def test_vanished_or_unusable_documents_are_not_retried_forever(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    _, fake = scheduler
    fake.extra_docs = [
        {"id": "0000deadbeef", "file_name": "gone.pdf", "category": "PAN card"},  # 404 now
        {"id": "bad.id/../x", "file_name": "odd.pdf", "category": "PAN card"},  # unusable id
    ]
    assert await _copy_report(scheduler, seed) == 2  # the two real files
    fetches = fake.content_fetches
    assert await _copy_report(scheduler, seed) == 0
    assert fake.content_fetches == fetches


async def test_commit_failure_after_upload_removes_the_bytes(
    scheduler: tuple[Any, FakeCircle],
    seed: _Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import circle_document_sync as sync_module
    from app.services.circle_document_sync import CopyOutcome

    module, _ = scheduler
    deleted: list[str | None] = []

    async def fake_copy_one(self: Any, *_: Any) -> CopyOutcome:
        return CopyOutcome(copied=True, object_key="Avora/workspace-files/k.pdf")

    async def fake_discard(key: str | None) -> None:
        deleted.append(key)

    class _FailingCommit:
        def __init__(self) -> None:
            self.real = module.SessionFactory

        def __call__(self) -> Any:
            session = self.real()

            async def boom() -> None:
                raise RuntimeError("commit failed")

            session.commit = boom
            return session

    monkeypatch.setattr(sync_module.CircleDocumentSync, "copy_one", fake_copy_one)
    monkeypatch.setattr(module, "discard_stored", fake_discard)
    monkeypatch.setattr(module, "SessionFactory", _FailingCommit())
    entry = _CIRCLE_PEOPLE["report@corp.test"]["documents"][0]
    with pytest.raises(RuntimeError):
        await module._copy_one(seed.report.id, "report@corp.test", entry)
    assert deleted == ["Avora/workspace-files/k.pdf"]


# --- profile: date of birth, gender, photo (fill only what is missing) ---------


async def _reload(db: AsyncSession, employee_id: Any) -> Employee:
    row = await db.scalar(
        select(Employee).where(Employee.id == employee_id).execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def test_fills_missing_birthday_gender_and_photo(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    module, _ = scheduler
    filled = await module.fill_profile_for(seed.report.id)
    assert filled == ["date_of_birth", "gender", "avatar"]
    report = await _reload(db, seed.report.id)
    assert str(report.date_of_birth) == "1998-04-12"
    assert report.gender is Gender.FEMALE
    assert report.has_avatar and report.avatar_content_type == "image/png"
    actions = (await db.scalars(select(AuditLog.action))).all()
    assert "profile.circle_fill" in actions


async def test_never_overwrites_what_avora_already_has(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    module, fake = scheduler
    report = await _reload(db, seed.report.id)
    report.date_of_birth = date(1990, 1, 1)
    report.gender = Gender.MALE
    report.avatar_content = b"existing"
    report.avatar_content_type = "image/jpeg"
    await db.commit()
    assert await module.fill_profile_for(seed.report.id) == []
    report = await _reload(db, seed.report.id)
    assert str(report.date_of_birth) == "1990-01-01" and report.gender is Gender.MALE
    assert report.avatar_content == b"existing"
    assert fake.avatar_fetches == 0


async def test_a_photo_that_is_not_an_image_is_tried_once(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    module, fake = scheduler
    fake.avatar = b"%PDF-1.4 not a photo"
    assert "avatar" not in await module.fill_profile_for(seed.report.id)
    await module.fill_profile_for(seed.report.id)
    assert fake.avatar_fetches == 1
    assert not (await _reload(db, seed.report.id)).has_avatar


async def test_oversized_photo_is_skipped(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    module, fake = scheduler
    fake.avatar = b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024)
    assert "avatar" not in await module.fill_profile_for(seed.report.id)
    assert not (await _reload(db, seed.report.id)).has_avatar


def test_gender_and_birth_date_parsing() -> None:
    assert map_gender(" Female ") is Gender.FEMALE
    assert map_gender("M") is Gender.MALE
    assert map_gender("prefer not to say") is None
    assert parse_birth_date("1998-04-12") == date(1998, 4, 12)
    assert parse_birth_date("2999-01-01") is None
    assert parse_birth_date("12/04/1998") is None


async def test_full_pass_also_fills_profiles(
    scheduler: tuple[Any, FakeCircle], seed: _Seed, db: AsyncSession
) -> None:
    module, _ = scheduler
    await module.run_once()
    assert (await _reload(db, seed.report.id)).date_of_birth is not None

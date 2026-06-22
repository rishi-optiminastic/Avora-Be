"""End-of-Day reports — generation (present/absent/idempotent), authorization,
and the approve→send recipients. Generation is tested at the service layer with a
stubbed LLM (Testing §9: no real network); authz is tested through the API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.activity import ActivitySample
from app.models.employee import Role
from app.models.eod_report import EodReport, EodStatus
from app.repositories.activity import ActivityRepository
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.eod_report import EodReportRepository
from app.repositories.notification import NotificationRepository
from app.repositories.regularization import RegularizationRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.task import TaskRepository
from app.schemas.auth import CurrentUser
from app.schemas.eod import EodDraftContent
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.attendance_service import AttendanceService
from app.services.eod_service import EodService
from app.services.notification_service import NotificationService
from tests.conftest import _Seed, auth_headers


class _StubLlm:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_eod(self, context: str) -> EodDraftContent:
        self.calls += 1
        return EodDraftContent(
            summary="Shipped the thing.", worked_on=["Avora"], tasks_completed=["t"], confidence=80
        )


class _CapturingEmail:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, *, to: str, subject: str, html: str) -> None:
        self.sent.append(to)


def _admin_caller(seed: _Seed) -> CurrentUser:
    return CurrentUser(employee_id=seed.admin.id, role=Role.ADMIN, manager_id=None)


def _build_service(
    db: AsyncSession, settings: Settings, *, llm: object, email: object
) -> EodService:
    eod_settings = settings.model_copy(
        update={"eod_enabled": True, "openrouter_api_key": "x", "eod_model": "test/model"}
    )
    audit = AuditRepository(db)
    employees = EmployeeRepository(db)
    policy = AttendancePolicyService(AttendancePolicyRepository(db), audit)
    attendance = AttendanceService(
        employees,
        ActivityRepository(db),
        # WorkSessionRepository is only needed for clock-in/out spans; absent here.
        _NoSessions(),  # type: ignore[arg-type]
        policy,
        RegularizationRepository(db),
    )
    return EodService(
        EodReportRepository(db),
        employees,
        TaskRepository(db),
        ActivityRepository(db),
        ScreenshotRepository(db),
        attendance,
        policy,
        llm,  # type: ignore[arg-type]
        email,  # type: ignore[arg-type]
        NotificationService(NotificationRepository(db)),
        audit,
        eod_settings,
    )


class _NoSessions:
    """Stub WorkSessionRepository — no biometric spans in these tests."""

    async def day_spans(
        self, employee_ids: object, start: object, end: object
    ) -> dict[uuid.UUID, object]:
        return {}


async def _add_activity(db: AsyncSession, seed: _Seed) -> None:
    """Two samples today for the report → attendance sees them as present."""
    now = datetime.now(UTC)
    for offset, seq in ((timedelta(hours=-3), 1), (timedelta(minutes=-5), 2)):
        db.add(
            ActivitySample(
                device_id=seed.device.id,
                employee_id=seed.report.id,
                sequence=seq,
                client_timestamp=now + offset,
                received_at=now + offset,
                active_window="VS Code",
                idle_seconds=0,
                flags=[],
            )
        )
    await db.commit()


async def test_absent_skipped_and_idempotent(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    llm = _StubLlm()
    service = _build_service(db, settings, llm=llm, email=_CapturingEmail())

    created = await service.generate_for_day(_admin_caller(seed), datetime.now(UTC))
    assert created == 0  # nobody has activity → everyone absent
    assert llm.calls == 0  # absent path never calls the LLM
    rows = (await db.execute(select(EodReport))).scalars().all()
    assert len(rows) == 4  # admin, manager, report, outsider
    assert all(r.status is EodStatus.SKIPPED_ABSENT for r in rows)

    # Second run is a no-op — no duplicate rows (the unique constraint holds).
    assert await service.generate_for_day(_admin_caller(seed), datetime.now(UTC)) == 0
    assert len((await db.execute(select(EodReport))).scalars().all()) == 4


async def test_present_generates_draft(db: AsyncSession, seed: _Seed, settings: Settings) -> None:
    await _add_activity(db, seed)
    llm = _StubLlm()
    service = _build_service(db, settings, llm=llm, email=_CapturingEmail())

    created = await service.generate_for_day(_admin_caller(seed), datetime.now(UTC))
    assert created == 1
    assert llm.calls == 1
    report = (
        await db.execute(select(EodReport).where(EodReport.employee_id == seed.report.id))
    ).scalar_one()
    assert report.status is EodStatus.DRAFT
    assert report.summary == "Shipped the thing."
    assert report.model == "test/model"


async def test_approve_emails_manager_and_admin(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    draft = EodReport(
        employee_id=seed.report.id,
        report_date="2026-06-22",
        status=EodStatus.DRAFT,
        summary="A day.",
        highlights={},
    )
    db.add(draft)
    await db.commit()

    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)
    report_caller = CurrentUser(
        employee_id=seed.report.id, role=Role.EMPLOYEE, manager_id=seed.manager.id
    )
    result = await service.approve(report_caller, draft.id)

    assert result.status == "sent"
    # manager + admin, never the employee themselves.
    assert set(email.sent) == {"manager@corp.test", "admin@corp.test"}


async def _seed_draft(db: AsyncSession, seed: _Seed) -> uuid.UUID:
    draft = EodReport(
        employee_id=seed.report.id,
        report_date="2026-06-22",
        status=EodStatus.DRAFT,
        summary="Draft body.",
        highlights={},
    )
    db.add(draft)
    await db.commit()
    return draft.id


async def test_read_is_scoped(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    report_id = await _seed_draft(db, seed)
    # The author and the manager (in scope) can read; the outsider cannot.
    assert (
        await client.get(f"/api/v1/eod/{report_id}", headers=auth_headers(settings, seed.report))
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/eod/{report_id}", headers=auth_headers(settings, seed.manager))
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/eod/{report_id}", headers=auth_headers(settings, seed.outsider))
    ).status_code == 404


async def test_only_author_can_edit(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    report_id = await _seed_draft(db, seed)
    body = {"summary": "My corrected summary."}
    # Manager may read but not edit someone else's report.
    assert (
        await client.patch(
            f"/api/v1/eod/{report_id}", json=body, headers=auth_headers(settings, seed.manager)
        )
    ).status_code == 404
    edited = await client.patch(
        f"/api/v1/eod/{report_id}", json=body, headers=auth_headers(settings, seed.report)
    )
    assert edited.status_code == 200
    assert edited.json()["summary"] == "My corrected summary."


async def test_generate_requires_admin(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    resp = await client.post("/api/v1/eod/generate", headers=auth_headers(settings, seed.report))
    assert resp.status_code == 403


async def test_cron_requires_secret(client: AsyncClient, seed: _Seed) -> None:
    # No secret configured in test settings → the endpoint 404s either way,
    # so it never runs unauthenticated and never leaks its existence.
    assert (await client.post("/api/v1/eod/cron")).status_code == 404
    wrong = await client.post("/api/v1/eod/cron", headers={"X-Cron-Secret": "nope"})
    assert wrong.status_code == 404

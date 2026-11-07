"""End-of-Day reports — generation (present/absent/idempotent), authorization,
and the approve→send recipients. Generation is tested at the service layer with a
stubbed LLM (Testing §9: no real network); authz is tested through the API."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.activity import ActivitySample
from app.models.employee import Employee, EmployeeStatus, Gender, Role
from app.models.eod_report import EodReport, EodStatus
from app.models.notification import Notification
from app.models.screenshot import Screenshot
from app.models.task import Task, TaskStatus
from app.repositories.activity import ActivityRepository
from app.repositories.attendance_override import AttendanceOverrideRepository
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.eod_report import EodReportRepository
from app.repositories.eod_settings import EodSettingsRepository
from app.repositories.holiday import HolidayRepository
from app.repositories.leave import LeaveRepository
from app.repositories.notification import NotificationRepository
from app.repositories.out_of_office import OutOfOfficeRepository
from app.repositories.regularization import RegularizationRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.task import TaskRepository
from app.schemas.auth import CurrentUser
from app.schemas.eod import (
    EodCumulativeTotals,
    EodDepartmentDigest,
    EodDepartmentMember,
    EodDraftContent,
)
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.attendance_service import AttendanceService
from app.services.email_templates import (
    eod_admin_digest_email,
    eod_group_digest_email,
    eod_report_email,
)
from app.services.eod_service import MAX_DIGEST_DAYS, UNASSIGNED_DEPARTMENT, EodService
from app.services.eod_settings_service import EodSettingsService
from app.services.llm_service import LlmError
from app.services.notification_service import NotificationService
from tests.conftest import _Seed, auth_headers


class _StubLlm:
    def __init__(self) -> None:
        self.calls = 0
        self.contexts: list[str] = []

    async def generate_eod(self, context: str) -> EodDraftContent:
        self.calls += 1
        self.contexts.append(context)
        return EodDraftContent(
            summary="Shipped the thing.", worked_on=["Avora"], tasks_completed=["t"], confidence=80
        )


class _FailingLlm:
    """Always errors — exercises the concurrent gather's failure → FAILED path."""

    async def generate_eod(self, context: str) -> EodDraftContent:
        raise LlmError("provider down")


class _CapturingEmail:
    """Records each message so tests can assert on recipients AND on content —
    the department rollup's whole point is what it does NOT carry."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.messages: list[dict[str, object]] = []

    async def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        cc: list[str] | None = None,
        attachments: list[object] | None = None,
        copy_hr: bool = False,
    ) -> None:
        del attachments, copy_hr  # EOD is not copied to HR; accepted so the
        # signature matches the real service and a future change fails loudly
        # here rather than looking like a mail outage.
        self.sent.append(to)
        self.messages.append({"to": to, "subject": subject, "html": html, "cc": cc or []})


def _admin_caller(seed: _Seed) -> CurrentUser:
    return CurrentUser(employee_id=seed.admin.id, role=Role.ADMIN, manager_id=None)


def _build_service(
    db: AsyncSession, settings: Settings, *, llm: object, email: object
) -> EodService:
    env = settings.model_copy(
        update={"eod_enabled": True, "openrouter_api_key": "x", "eod_model": "test/model"}
    )
    audit = AuditRepository(db)
    employees = EmployeeRepository(db)
    # The DB schedule seeds from env on first read, so this row comes up enabled
    # with the default 16:30 draft / 18:00 send used by the window tests.
    eod_settings = EodSettingsService(EodSettingsRepository(db), audit, env)
    policy = AttendancePolicyService(AttendancePolicyRepository(db), audit)
    attendance = AttendanceService(
        employees,
        ActivityRepository(db),
        # WorkSessionRepository is only needed for clock-in/out spans; absent here.
        _NoSessions(),  # type: ignore[arg-type]
        policy,
        RegularizationRepository(db),
        AttendanceOverrideRepository(db),
        HolidayRepository(db),
        LeaveRepository(db),
        OutOfOfficeRepository(db),
        AuditRepository(db),
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
        env,
    )


class _NoSessions:
    """Stub WorkSessionRepository — no work sessions of any kind in these tests
    (attendance falls back to activity)."""

    async def day_spans(
        self, employee_ids: object, start: object, end: object
    ) -> dict[uuid.UUID, object]:
        return {}

    async def biometric_day_spans(
        self, employee_ids: object, start: object, end: object
    ) -> dict[uuid.UUID, object]:
        return {}

    async def sessions_in_range(
        self, employee_ids: object, start: object, end: object, source: object = None
    ) -> list[object]:
        # Read by the monthly late-arrival count; no sessions here either.
        return []


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


def test_pronoun_for_maps_gender() -> None:
    from app.services.eod_service import _pronoun_for

    assert _pronoun_for(Gender.MALE) == "he/him"
    assert _pronoun_for(Gender.FEMALE) == "she/her"
    assert _pronoun_for(None) == "they/them"  # unknown → neutral, never a name guess


async def test_eod_context_carries_employee_pronouns(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    report = await db.get(Employee, seed.report.id)
    assert report is not None
    report.gender = Gender.MALE
    await db.commit()
    await _add_activity(db, seed)

    llm = _StubLlm()
    service = _build_service(db, settings, llm=llm, email=_CapturingEmail())
    await service.generate_for_day(_admin_caller(seed), datetime.now(UTC))

    # The model is told the correct pronouns, so it can't guess 'she' from the name.
    assert any("he/him" in ctx for ctx in llm.contexts)
    assert not any("she/her" in ctx for ctx in llm.contexts)


async def test_failed_llm_marks_failed(db: AsyncSession, seed: _Seed, settings: Settings) -> None:
    await _add_activity(db, seed)
    service = _build_service(db, settings, llm=_FailingLlm(), email=_CapturingEmail())

    created = await service.generate_for_day(_admin_caller(seed), datetime.now(UTC))
    assert created == 0  # the LLM failed for the one present employee
    report = (
        await db.execute(select(EodReport).where(EodReport.employee_id == seed.report.id))
    ).scalar_one()
    assert report.status is EodStatus.FAILED


async def test_purge_old_activity(db: AsyncSession, seed: _Seed, settings: Settings) -> None:
    now = datetime.now(UTC)
    db.add(
        ActivitySample(
            device_id=seed.device.id,
            employee_id=seed.report.id,
            sequence=50,
            client_timestamp=now - timedelta(days=40),
            received_at=now - timedelta(days=40),  # older than the 30-day window
            idle_seconds=0,
            flags=[],
        )
    )
    db.add(
        ActivitySample(
            device_id=seed.device.id,
            employee_id=seed.report.id,
            sequence=51,
            client_timestamp=now,
            received_at=now,  # recent — must survive
            idle_seconds=0,
            flags=[],
        )
    )
    await db.commit()

    service = _build_service(db, settings, llm=_StubLlm(), email=_CapturingEmail())
    purged = await service.purge_old_activity(now)
    assert purged == 1
    survivors = (await db.execute(select(ActivitySample))).scalars().all()
    assert [s.sequence for s in survivors] == [51]


async def test_purge_old_screenshots(db: AsyncSession, seed: _Seed, settings: Settings) -> None:
    now = datetime.now(UTC)
    for received_at, byte_size in ((now - timedelta(days=40), 1), (now, 2)):
        db.add(
            Screenshot(
                device_id=seed.device.id,
                employee_id=seed.report.id,
                captured_at=received_at,
                received_at=received_at,
                content_type="image/jpeg",
                width=100,
                height=100,
                byte_size=byte_size,  # marks which row is which
                object_key=None,
                image=b"x",
                flags=[],
            )
        )
    await db.commit()

    # No S3 in tests, so this is a DB-only purge (blob deletion is skipped).
    service = _build_service(db, settings, llm=_StubLlm(), email=_CapturingEmail())
    purged = await service.purge_old_screenshots(now)
    assert purged == 1
    survivors = (await db.execute(select(Screenshot))).scalars().all()
    assert [s.byte_size for s in survivors] == [2]  # only the recent one remains


async def test_approve_emails_only_the_reporting_manager(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """A person's own report goes to them, and to nobody else at that moment.

    The manager is not mailed per report any more: they get one team rollup at
    the send time, carrying this narrative. Admins were once copied on every
    individual report, which contradicted `_personal_scope_clause` - they were
    mailed a narrative they are not allowed to open in the app.
    """
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
        employee_id=seed.report.id, role=Role.EXECUTIVE, manager_id=seed.manager.id
    )
    result = await service.approve(report_caller, draft.id)

    assert result.status == "sent"
    assert email.sent == ["report@corp.test"]
    assert email.messages[0]["subject"] == "Your end of day · Mon, 22 Jun"
    assert "admin@corp.test" not in email.sent and "manager@corp.test" not in email.sent


async def test_report_with_no_manager_still_reaches_its_author(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """The outsider has no `manager_id`. Their report must not fall back to the
    admins - that is the leak we removed - and it must not get stuck as a draft
    either: it goes to them, and they stay visible to admins via the rollup."""
    draft = EodReport(
        employee_id=seed.outsider.id,
        report_date="2026-06-22",
        status=EodStatus.DRAFT,
        summary="A day with no manager on file.",
        highlights={},
    )
    db.add(draft)
    await db.commit()

    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)
    caller = CurrentUser(employee_id=seed.outsider.id, role=Role.EXECUTIVE, manager_id=None)
    result = await service.approve(caller, draft.id)

    assert result.status == "sent"
    assert email.sent == ["outsider@corp.test"]


async def test_auto_send_respects_the_6pm_window(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime.now(UTC)
    before = datetime(2026, 6, 29, 17, 0, tzinfo=tz)  # 5:00 PM IST — before send time
    after = datetime(2026, 6, 29, 18, 30, tzinfo=tz)  # 6:30 PM IST — past send time
    today = before.date().isoformat()
    yesterday = (before.date() - timedelta(days=1)).isoformat()
    for report_date in (today, yesterday):
        db.add(
            EodReport(
                employee_id=seed.report.id,
                report_date=report_date,
                status=EodStatus.DRAFT,
                summary="x",
                highlights={},
            )
        )
    await db.commit()

    service = _build_service(db, settings, llm=_StubLlm(), email=_CapturingEmail())

    # Before 6 PM NOTHING goes out — not even the prior-day leftover. EOD reports
    # never leave outside the send window (so a stray draft can't fire at 3:40 PM).
    assert await service.auto_send_due(now, before) == 0
    # At/after 6 PM both the leftover prior-day draft and today's are flushed.
    assert await service.auto_send_due(now, after) == 2
    rows = (
        (await db.execute(select(EodReport).where(EodReport.employee_id == seed.report.id)))
        .scalars()
        .all()
    )
    assert all(r.status is EodStatus.SENT for r in rows)


async def test_send_drafts_for_date_sends_only_that_day(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime.now(UTC)
    local = datetime(2026, 6, 29, 19, 0, tzinfo=tz)
    today = local.date().isoformat()
    yesterday = (local.date() - timedelta(days=1)).isoformat()
    for report_date in (today, yesterday):
        db.add(
            EodReport(
                employee_id=seed.report.id,
                report_date=report_date,
                status=EodStatus.DRAFT,
                summary="x",
                highlights={},
            )
        )
    await db.commit()

    service = _build_service(db, settings, llm=_StubLlm(), email=_CapturingEmail())

    # Only today's draft is flushed; the earlier-day backlog is left untouched.
    assert await service.send_drafts_for_date(today, now) == 1
    rows = {
        r.report_date: r.status
        for r in (
            await db.execute(select(EodReport).where(EodReport.employee_id == seed.report.id))
        )
        .scalars()
        .all()
    }
    assert rows[today] is EodStatus.SENT
    assert rows[yesterday] is EodStatus.DRAFT


async def test_draft_notice_tells_employee_the_auto_send_time(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    await _add_activity(db, seed)
    service = _build_service(db, settings, llm=_StubLlm(), email=_CapturingEmail())
    assert await service.generate_for_day(_admin_caller(seed), datetime.now(UTC)) == 1

    note = (
        await db.execute(select(Notification).where(Notification.recipient_id == seed.report.id))
    ).scalar_one()
    assert "drafted" in note.title.lower()
    # The employee is told it auto-sends (the exact time label is tz-derived).
    assert "automatically at" in (note.body or "")


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


async def test_eod_settings_seed_guards_against_an_inverted_env(
    db: AsyncSession, settings: Settings
) -> None:
    # A bare EOD_REPORT_HOUR=18 with the default 18:00 send would put the draft
    # after the send (no review window) — the seed must fall back to sane defaults.
    env = settings.model_copy(update={"eod_report_hour": 18, "eod_report_minute": 30})
    service = EodSettingsService(EodSettingsRepository(db), AuditRepository(db), env)
    sched = await service.spec()
    assert (sched.draft_hour, sched.draft_minute) == (16, 30)
    assert (sched.send_hour, sched.send_minute) == (18, 0)


async def test_eod_settings_read_open_write_admin_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Anyone authenticated can read the schedule (they see when their report sends).
    read = await client.get("/api/v1/eod/settings", headers=auth_headers(settings, seed.report))
    assert read.status_code == 200
    assert read.json()["send_time"] == "18:00"  # seeded from env defaults
    # …but only admin/HR may change it.
    body = {"enabled": True, "draft_time": "17:00", "send_time": "19:30"}
    assert (
        await client.put(
            "/api/v1/eod/settings", json=body, headers=auth_headers(settings, seed.report)
        )
    ).status_code == 403
    ok = await client.put(
        "/api/v1/eod/settings", json=body, headers=auth_headers(settings, seed.admin)
    )
    assert ok.status_code == 200
    assert ok.json() == {"enabled": True, "draft_time": "17:00", "send_time": "19:30"}
    # A send time at or before the draft time is rejected (no review window).
    bad = await client.put(
        "/api/v1/eod/settings",
        json={"send_time": "16:00"},
        headers=auth_headers(settings, seed.admin),
    )
    assert bad.status_code == 409


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


def _local_today() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()


async def _seed_history(db: AsyncSession, seed: _Seed) -> None:
    """Two sent reports for the report employee on consecutive recent days."""
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    for offset in (0, 1):
        day = (today - timedelta(days=offset)).isoformat()
        db.add(
            EodReport(
                employee_id=seed.report.id,
                report_date=day,
                status=EodStatus.SENT,
                summary=f"Day {day}.",
                highlights={"tasks_completed": ["t1"], "blockers": ["b1"]},
                metrics={"worked_minutes": 480, "active_minutes": 300, "tasks_done": 1},
            )
        )
    await db.commit()


async def test_history_is_scoped_and_newest_first(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    await _seed_history(db, seed)

    # The author's own history (defaults to the caller) — newest first.
    own = await client.get("/api/v1/eod/history", headers=auth_headers(settings, seed.report))
    assert own.status_code == 200
    dates = [r["report_date"] for r in own.json()]
    assert dates == sorted(dates, reverse=True) and len(dates) == 2

    # The manager (report is a direct report) may read it by employee_id…
    mgr = await client.get(
        f"/api/v1/eod/history?employee_id={seed.report.id}",
        headers=auth_headers(settings, seed.manager),
    )
    assert mgr.status_code == 200 and len(mgr.json()) == 2

    # …the outsider may not — 404, never revealing the reports exist.
    outsider = await client.get(
        f"/api/v1/eod/history?employee_id={seed.report.id}",
        headers=auth_headers(settings, seed.outsider),
    )
    assert outsider.status_code == 404


async def test_cumulative_is_scoped_with_coverage_and_totals(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    today = _local_today()
    db.add(
        EodReport(
            employee_id=seed.report.id,
            report_date=today,
            status=EodStatus.SENT,
            summary="Shipped.",
            highlights={"tasks_completed": ["t1", "t2"], "blockers": ["b1"]},
            metrics={"worked_minutes": 480, "active_minutes": 300, "tasks_done": 2},
        )
    )
    await db.commit()

    # The manager's digest covers self + direct report: one submitted, the rest
    # missing, with the submitted report's effort summed into the totals.
    mgr = await client.get("/api/v1/eod/cumulative", headers=auth_headers(settings, seed.manager))
    assert mgr.status_code == 200
    body = mgr.json()
    assert body["from_date"] == today and body["to_date"] == today
    assert body["submitted"] == 1
    assert body["totals"] == {
        "worked_minutes": 480,
        "active_minutes": 300,
        "tasks_done": 2,
        "blockers": 1,
    }
    assert any(r["employee_id"] == str(seed.report.id) for r in body["reports"])
    covered = {m["employee_id"]: m["coverage"] for m in body["members"]}
    assert covered[str(seed.report.id)] == "submitted"

    # The outsider's digest is scoped to themselves — the report never appears.
    out = await client.get("/api/v1/eod/cumulative", headers=auth_headers(settings, seed.outsider))
    assert out.status_code == 200
    out_body = out.json()
    assert out_body["submitted"] == 0 and out_body["reports"] == []
    assert all(m["employee_id"] != str(seed.report.id) for m in out_body["members"])


# --------------------------------------------------------------------------- #
# The admins' rollup — one email covering every department, now that individual
# reports go only to the reporting manager.
# --------------------------------------------------------------------------- #
_IST = ZoneInfo("Asia/Kolkata")
# A fixed local evening, past the default 18:00 send time, so the window gate and
# the report date are both deterministic.
_EVENING = datetime(2026, 6, 29, 19, 0, tzinfo=_IST)
_DIGEST_DAY = _EVENING.date().isoformat()


async def _add_department(db: AsyncSession, department: str, *names: str) -> list[Employee]:
    """Active employees in one department, with no reports filed yet."""
    people = [
        Employee(
            hr_external_id=f"hr-{name.lower()}",
            work_email=f"{name.lower()}@corp.test",
            full_name=name.title(),
            role=Role.EXECUTIVE,
            department=department,
            status=EmployeeStatus.ACTIVE,
            is_active=True,
        )
        for name in names
    ]
    db.add_all(people)
    await db.commit()
    return people


def _admin_digest(email: _CapturingEmail) -> dict[str, object]:
    """The one end-of-day rollup the admins were sent."""
    matches = [m for m in email.messages if str(m["subject"]).startswith("EOD · ")]
    assert len(matches) == 1, f"expected one admin digest, got {len(matches)}"
    return matches[0]


async def _headcount(db: AsyncSession) -> int:
    rows = await db.execute(select(Employee).where(Employee.is_active.is_(True)))
    return len(rows.scalars().all())


async def test_admin_digest_is_one_email_covering_every_department(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """One message, not one per department: ten near-identical mails at six
    o'clock read as noise. Every department is inside it, including the bucket
    holding the seed people who have no department on file - they must not
    vanish from the admins' view."""
    await _add_department(db, "Design", "dia", "devi")
    await _add_department(db, "Sales", "sam")
    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)

    count = await service.send_digests(datetime.now(UTC), _EVENING)

    # The company rollup, plus one team rollup per direct manager (the seed's).
    assert count >= 1
    message = _admin_digest(email)
    html = str(message["html"])
    for department in ("Design", "Sales", UNASSIGNED_DEPARTMENT):
        assert department in html
    # The subject carries the company-wide figure, not any one department's.
    assert message["subject"] == f"EOD · Mon, 29 Jun · 0/{await _headcount(db)} reported"
    # To the admin, and to nobody who isn't one.
    assert message["to"] == "admin@corp.test"
    assert message["cc"] == []  # only one admin is seeded


async def test_admin_digest_leads_with_the_company_figure_and_indexes_departments(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """The first number is the company's; an index then says which department
    to open, and each department keeps its own figure in its section below."""
    (dia, _devi) = await _add_department(db, "Design", "dia", "devi")
    await _add_department(db, "Sales", "sam")
    db.add(
        EodReport(
            employee_id=dia.id,
            report_date=_DIGEST_DAY,
            status=EodStatus.SENT,
            summary="x",
            highlights={},
            metrics={"worked_minutes": 120, "active_minutes": 60},
        )
    )
    await db.commit()

    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)
    await service.send_digests(datetime.now(UTC), _EVENING)
    html = str(_admin_digest(email)["html"])

    company = f"1 of {await _headcount(db)} reported"
    assert company in html
    assert "1 of 2 reported" in html  # Design's own heading
    assert "0 of 1 reported" in html  # Sales
    assert 'href="#dept-design"' in html and 'id="dept-design"' in html
    assert 'href="#dept-sales"' in html and 'id="dept-sales"' in html
    # The company figure comes before any department's section.
    assert html.index(company) < html.index('id="dept-design"')


def test_eod_emails_are_laid_out_for_a_phone() -> None:
    """Both EOD emails declare a viewport and carry the phone-width rules.

    Without the meta a phone client lays the mail out at desktop width and
    scales it down until the type cannot be read - which is what the old
    six-column department rollup did on every phone it reached.
    """
    _subject, report = eod_report_email(
        employee_name="Dia", date_label=_DIGEST_DAY, summary="A day.", highlights=EodDraftContent()
    )
    digest = EodDepartmentDigest(
        from_date=_DIGEST_DAY,
        to_date=_DIGEST_DAY,
        member_count=1,
        submitted=1,
        pending=0,
        absent=0,
        failed=0,
        missing=0,
        submission_rate=1.0,
        working_days=1,
        submitted_days=1,
        totals=EodCumulativeTotals(worked_minutes=60, active_minutes=30, tasks_done=1),
        department="Design",
        members=[
            EodDepartmentMember(
                employee_id=uuid.uuid4(),
                name="Dia",
                coverage="submitted",
                worked_minutes=60,
                active_minutes=30,
                active_pct=50,
                tasks_done=1,
            )
        ],
        blockers=[],
    )
    _subject, rollup = eod_admin_digest_email(digests=[digest], report_date=_DIGEST_DAY)
    _subject, team = eod_group_digest_email(
        kind="team", label="Max Manager", digest=digest, narratives={}, report_date=_DIGEST_DAY
    )

    for html in (report, rollup, team):
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
        assert "@media only screen and (max-width: 600px)" in html


async def test_admin_digest_carries_the_numbers_and_blockers_but_no_narrative(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """The privacy contract: an admin learns who reported and what the day added
    up to, never what anyone wrote about their day."""
    (dia, _devi) = await _add_department(db, "Design", "dia", "devi")
    db.add(
        EodReport(
            employee_id=dia.id,
            report_date=_DIGEST_DAY,
            status=EodStatus.SENT,
            summary="PRIVATE-NARRATIVE-DO-NOT-SHARE: spent the day on the brand refresh.",
            highlights={
                "summary": "PRIVATE-NARRATIVE-DO-NOT-SHARE",
                "tasks_completed": ["Logo pass", "Type scale"],
                "worked_on": ["Brand refresh"],
                "blockers": ["Waiting on the copy deck"],
            },
            metrics={"worked_minutes": 480, "active_minutes": 360, "tasks_done": 2},
        )
    )
    await db.commit()

    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)
    await service.send_digests(datetime.now(UTC), _EVENING)
    html = str(_admin_digest(email)["html"])

    # The narrative never appears — not the edited summary, not the draft copy of
    # it inside `highlights`.
    assert "PRIVATE-NARRATIVE-DO-NOT-SHARE" not in html
    # The work shipped IS named - a completed task is an organisational fact that
    # already sits on the board, unlike the narrative above.
    assert "Logo pass" in html and "Type scale" in html
    # What IS there: the roster and the department's blocker. Time on the
    # machine is deliberately absent: the admins asked for tasks and blockers.
    assert "Dia" in html and "Devi" in html
    assert "8.0h" not in html and "on the machine" not in html
    assert "Waiting on the copy deck" in html
    # Coverage: one of the two Design people reported.
    assert "1 of 2 reported" in html


async def test_admin_digest_sends_once_per_local_day(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """The scheduler ticks every 15 minutes and re-flushes pending drafts; without
    the date marker the admins would be mailed again on every tick till midnight."""
    await _add_department(db, "Design", "dia")
    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)

    assert await service.send_digests(datetime.now(UTC), _EVENING) >= 1
    before = len(email.messages)
    # A later tick the same evening sends nothing more…
    assert await service.send_digests(datetime.now(UTC), _EVENING.replace(hour=21)) == 0
    assert len(email.messages) == before
    # …but the next day is a fresh run.
    assert await service.send_digests(datetime.now(UTC), _EVENING + timedelta(days=1)) >= 1


async def test_admin_digest_waits_for_the_send_window(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    await _add_department(db, "Design", "dia")
    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)

    # 5 PM IST — before the 18:00 send time. Nothing leaves, same hard gate the
    # individual reports obey.
    assert await service.send_digests(datetime.now(UTC), _EVENING.replace(hour=17)) == 0
    assert email.messages == []


async def test_admin_digest_counts_a_draft_flushed_in_the_same_tick(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """Ordering guard: the digest runs after the individual auto-send, so a draft
    flushed moments ago reads as submitted rather than still pending."""
    (dia,) = await _add_department(db, "Design", "dia")
    db.add(
        EodReport(
            employee_id=dia.id,
            report_date=_DIGEST_DAY,
            status=EodStatus.DRAFT,  # still pending when the tick starts
            summary="A day.",
            highlights={},
            metrics={"worked_minutes": 60, "active_minutes": 30},
        )
    )
    await db.commit()

    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)
    await service.auto_send_due(datetime.now(UTC), _EVENING)
    await service.send_digests(datetime.now(UTC), _EVENING)

    html = str(_admin_digest(email)["html"])
    assert "1 of 1 reported" in html
    assert "Pending" not in html


async def _add_person(
    db: AsyncSession, name: str, role: Role, department: str | None = None
) -> Employee:
    person = Employee(
        hr_external_id=f"hr-{name.lower()}",
        work_email=f"{name.lower()}@corp.test",
        full_name=name.title(),
        role=role,
        department=department,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(person)
    await db.commit()
    return person


async def test_departments_endpoint_is_role_gated(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    await _add_department(db, "Design", "dia")
    await _add_department(db, "Sales", "sam")

    # An ordinary employee may not read across departments at all - their own
    # team digest is /eod/cumulative. Nor may a plain manager.
    for caller in (seed.report, seed.manager, seed.outsider):
        denied = await client.get("/api/v1/eod/departments", headers=auth_headers(settings, caller))
        assert denied.status_code == 403

    # The admin sees every department.
    admin = await client.get("/api/v1/eod/departments", headers=auth_headers(settings, seed.admin))
    assert admin.status_code == 200
    assert {d["department"] for d in admin.json()} == {"Design", "Sales", UNASSIGNED_DEPARTMENT}


async def test_departments_endpoint_gives_hr_every_department(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    await _add_department(db, "Design", "dia")
    await _add_department(db, "Sales", "sam")
    hr = await _add_person(db, "hana", Role.HR)

    res = await client.get("/api/v1/eod/departments", headers=auth_headers(settings, hr))
    assert res.status_code == 200
    assert {d["department"] for d in res.json()} == {"Design", "Sales", UNASSIGNED_DEPARTMENT}


async def test_departments_endpoint_scopes_a_senior_manager_to_their_department(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    (dia,) = await _add_department(db, "Design", "dia")
    (sam,) = await _add_department(db, "Sales", "sam")
    lead = await _add_person(db, "lena", Role.SENIOR_MANAGER, "Design")

    res = await client.get("/api/v1/eod/departments", headers=auth_headers(settings, lead))
    assert res.status_code == 200
    body = res.json()
    # Only their own department, and nobody from another one leaks in.
    assert [d["department"] for d in body] == ["Design"]
    ids = {m["employee_id"] for m in body[0]["members"]}
    assert ids == {str(dia.id), str(lead.id)}
    assert str(sam.id) not in res.text

    # A senior manager with no department on file sees nothing, not the
    # unassigned bucket of the whole org.
    floating = await _add_person(db, "flo", Role.SENIOR_MANAGER)
    none = await client.get("/api/v1/eod/departments", headers=auth_headers(settings, floating))
    assert none.status_code == 200 and none.json() == []


async def test_department_coverage_is_per_day_not_per_person(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """One report in a week is one day of five, not a fully covered week."""
    (dia, _devi) = await _add_department(db, "Design", "dia", "devi")
    # 2026-06-29 is a Monday: Mon..Fri 2026-06-29..07-03 is five working days.
    db.add(
        EodReport(
            employee_id=dia.id,
            report_date=_DIGEST_DAY,
            status=EodStatus.SENT,
            summary="A day.",
            highlights={},
        )
    )
    await db.commit()

    caller = CurrentUser(employee_id=seed.admin.id, role=Role.ADMIN, manager_id=None)
    service = _build_service(db, settings, llm=_StubLlm(), email=_CapturingEmail())
    (design,) = [
        d
        for d in await service.department_digests(
            caller, "2026-06-27", "2026-07-03", datetime.now(UTC)
        )
        if d.department == "Design"
    ]
    assert design.working_days == 5  # the weekend is not a day anyone owed a report
    assert design.submitted_days == 1
    assert design.submission_rate == 0.1  # 1 of 2 people x 5 days
    reported = next(m for m in design.members if m.employee_id == dia.id)
    assert reported.submitted_days == 1


async def test_departments_days_window_is_resolved_in_the_org_timezone(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    await _add_department(db, "Design", "dia")
    res = await client.get(
        "/api/v1/eod/departments?days=7", headers=auth_headers(settings, seed.admin)
    )
    assert res.status_code == 200
    today = datetime.fromisoformat(_local_today()).date()
    design = next(d for d in res.json() if d["department"] == "Design")
    assert design["to_date"] == today.isoformat()
    assert design["from_date"] == (today - timedelta(days=6)).isoformat()
    # Out-of-range spans are rejected by validation, never clamped silently.
    too_long = await client.get(
        "/api/v1/eod/departments?days=400", headers=auth_headers(settings, seed.admin)
    )
    assert too_long.status_code == 422


async def test_team_digest_days_window_is_resolved_in_the_org_timezone(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    """The team page asks for "last N days" and lets the server pick the dates,
    so a UTC server never shifts an IST team's window by a day."""
    res = await client.get(
        "/api/v1/eod/cumulative?days=7", headers=auth_headers(settings, seed.manager)
    )
    assert res.status_code == 200
    today = datetime.fromisoformat(_local_today()).date()
    assert res.json()["to_date"] == today.isoformat()
    assert res.json()["from_date"] == (today - timedelta(days=6)).isoformat()
    assert "working_days" in res.json() and "submitted_days" in res.json()
    too_long = await client.get(
        "/api/v1/eod/cumulative?days=400", headers=auth_headers(settings, seed.manager)
    )
    assert too_long.status_code == 422


_DIGEST_ENDPOINTS = ("/api/v1/eod/cumulative", "/api/v1/eod/departments")


async def test_digest_explicit_window_far_future_to_is_clamped_to_today(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    """`to=9999-12-31` used to overflow `date` (500). It is clamped to the org's
    today instead, so a short window ending "in the future" still works."""
    today = datetime.fromisoformat(_local_today()).date()
    start = (today - timedelta(days=3)).isoformat()
    for path in _DIGEST_ENDPOINTS:
        res = await client.get(
            f"{path}?from={start}&to=9999-12-31", headers=auth_headers(settings, seed.admin)
        )
        assert res.status_code == 200, (path, res.text)
        body = res.json()
        windows = [body] if isinstance(body, dict) else body
        for window in windows:
            assert window["from_date"] == start and window["to_date"] == today.isoformat()


async def test_digest_explicit_window_longer_than_the_cap_is_rejected(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    """An explicit `from`/`to` is held to the same cap as `days`, so a year
    0001-to-9999 window is a 422 rather than a multi-second walk of every day."""
    today = datetime.fromisoformat(_local_today()).date()
    at_cap = (today - timedelta(days=MAX_DIGEST_DAYS - 1)).isoformat()
    over_cap = (today - timedelta(days=MAX_DIGEST_DAYS)).isoformat()
    for path in _DIGEST_ENDPOINTS:
        huge = await client.get(
            f"{path}?from=0001-01-01&to=9999-12-30", headers=auth_headers(settings, seed.admin)
        )
        assert huge.status_code == 422, path
        over = await client.get(
            f"{path}?from={over_cap}&to={today}", headers=auth_headers(settings, seed.admin)
        )
        assert over.status_code == 422, path
        ok = await client.get(
            f"{path}?from={at_cap}&to={today}", headers=auth_headers(settings, seed.admin)
        )
        assert ok.status_code == 200, path


async def test_digest_reversed_or_future_window_is_rejected(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    today = datetime.fromisoformat(_local_today()).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    for path in _DIGEST_ENDPOINTS:
        reversed_ = await client.get(
            f"{path}?from={today}&to={yesterday}", headers=auth_headers(settings, seed.admin)
        )
        assert reversed_.status_code == 422, path
        future = await client.get(
            f"{path}?from={tomorrow}&to={tomorrow}", headers=auth_headers(settings, seed.admin)
        )
        assert future.status_code == 422, path


async def test_working_dates_is_safe_at_the_end_of_the_calendar(
    db: AsyncSession, settings: Settings
) -> None:
    """Stepping one day past `date.max` overflowed; the walk must stop at it."""
    audit = AuditRepository(db)
    service = AttendanceService(
        EmployeeRepository(db),
        ActivityRepository(db),
        _NoSessions(),  # type: ignore[arg-type]
        AttendancePolicyService(AttendancePolicyRepository(db), audit),
        RegularizationRepository(db),
        AttendanceOverrideRepository(db),
        HolidayRepository(db),
        LeaveRepository(db),
        OutOfOfficeRepository(db),
        audit,
    )
    last = date.max
    working = await service.working_dates(last - timedelta(days=6), last)
    assert working and working[-1] <= last and len(working) <= 7


async def test_departments_endpoint_reports_coverage_without_narratives(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    (dia, _devi) = await _add_department(db, "Design", "dia", "devi")
    today = _local_today()
    db.add(
        EodReport(
            employee_id=dia.id,
            report_date=today,
            status=EodStatus.SENT,
            summary="PRIVATE-NARRATIVE",
            highlights={"tasks_completed": ["t1"], "blockers": ["Blocked on copy"]},
            metrics={"worked_minutes": 480, "active_minutes": 240, "tasks_done": 1},
        )
    )
    await db.commit()

    res = await client.get("/api/v1/eod/departments", headers=auth_headers(settings, seed.admin))
    assert res.status_code == 200
    design = next(d for d in res.json() if d["department"] == "Design")

    assert design["member_count"] == 2
    assert design["submitted"] == 1 and design["missing"] == 1
    assert design["submission_rate"] == 0.5
    assert design["totals"]["tasks_done"] == 1
    assert design["blockers"] == ["Blocked on copy"]
    # No field anywhere in the payload carries the narrative.
    assert "PRIVATE-NARRATIVE" not in res.text
    reported = next(m for m in design["members"] if m["employee_id"] == str(dia.id))
    assert reported["coverage"] == "submitted"
    assert reported["worked_minutes"] == 480 and reported["active_pct"] == 50


async def test_admin_digest_names_the_work_shipped_but_caps_the_list(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """Titles appear under each person, de-duped across the window and capped so
    one busy day cannot turn a roster row into half the email."""
    (dia,) = await _add_department(db, "Design", "dia")
    for day, titles in (
        (_DIGEST_DAY, ["Logo pass", "Type scale", "Icon set", "Spacing audit", "Dark mode"]),
        # A second day repeating a title: the department wants distinct work.
        ((_EVENING - timedelta(days=1)).date().isoformat(), ["Logo pass", "Colour tokens"]),
    ):
        db.add(
            EodReport(
                employee_id=dia.id,
                report_date=day,
                status=EodStatus.SENT,
                summary="A day.",
                highlights={"tasks_completed": titles},
                metrics={"worked_minutes": 480, "active_minutes": 360},
            )
        )
    await db.commit()

    caller = CurrentUser(employee_id=seed.admin.id, role=Role.ADMIN, manager_id=None)
    week_start = (_EVENING - timedelta(days=6)).date().isoformat()
    (design,) = [
        d
        for d in await _build_service(
            db, settings, llm=_StubLlm(), email=_CapturingEmail()
        ).department_digests(caller, week_start, _DIGEST_DAY, datetime.now(UTC))
        if d.department == "Design"
    ]
    member = design.members[0]
    # Six distinct titles across two days - "Logo pass" counted once.
    assert member.tasks.count("Logo pass") == 1
    assert len(member.tasks) == 6

    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)
    await service.send_digests(datetime.now(UTC), _EVENING)
    html = str(_admin_digest(email)["html"])
    # Only the first three are named; the rest are counted, not listed.
    assert "Logo pass" in html and "Type scale" in html and "Icon set" in html
    assert "Spacing audit" not in html
    assert "+2 more" in html


async def test_admin_digest_names_who_still_owes_but_not_the_absent(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """ "Still owed" is for people who could have reported and have not.

    Someone marked absent legitimately has no report, so dunning them for one is
    how a daily summary starts feeling like surveillance. Mirrors the dashboard's
    own bands: PENDING and MISSING only.
    """
    pending_one, absent_one, _missing_one = await _add_department(
        db, "Design", "dia", "devi", "dara"
    )
    db.add(
        EodReport(
            employee_id=pending_one.id,
            report_date=_DIGEST_DAY,
            status=EodStatus.DRAFT,  # drafted, not submitted -> pending -> owed
            summary="x",
            highlights={},
        )
    )
    db.add(
        EodReport(
            employee_id=absent_one.id,
            report_date=_DIGEST_DAY,
            status=EodStatus.SKIPPED_ABSENT,  # absent -> NOT owed
        )
    )
    await db.commit()

    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)
    # Send without flushing drafts first, so the pending row stays pending.
    await service.send_digests(datetime.now(UTC), _EVENING)
    html = str(_admin_digest(email)["html"])

    # The "Needs attention" box names who still owes a report, per department.
    attention = html.split("Needs attention")[1].split("</table>")[0]
    assert "Dia" in attention  # pending
    assert "Dara" in attention  # no report at all
    assert "Devi" not in attention  # absent - never chased


async def test_department_digest_counts_what_the_day_left_open(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """Open and overdue come from the task board, not the report.

    A department can ship well and still be falling behind; `tasks_done` counts
    what the day closed, these count what it left standing. Only both numbers
    side by side show the difference.
    """
    (dia,) = await _add_department(db, "Design", "dia")
    now = datetime.now(UTC)
    for status, due in (
        (TaskStatus.TODO, now - timedelta(days=3)),  # open AND overdue
        (TaskStatus.IN_PROGRESS, now + timedelta(days=5)),  # open, on time
        (TaskStatus.BLOCKED, None),  # open, no due date -> never overdue
        (TaskStatus.DONE, now - timedelta(days=9)),  # closed -> counted nowhere
    ):
        db.add(
            Task(
                title="t",
                assignee_id=dia.id,
                assigned_by_id=seed.manager.id,
                status=status,
                due_date=due,
            )
        )
    await db.commit()

    caller = CurrentUser(employee_id=seed.admin.id, role=Role.ADMIN, manager_id=None)
    service = _build_service(db, settings, llm=_StubLlm(), email=_CapturingEmail())
    (design,) = [
        d
        for d in await service.department_digests(caller, None, None, now)
        if d.department == "Design"
    ]
    member = design.members[0]
    assert member.tasks_open == 3  # todo + in_progress + blocked, never the done one
    assert member.tasks_overdue == 1  # only the past-due one; no due date is not late
    assert design.open_tasks == 3 and design.overdue_tasks == 1


async def test_an_it_admin_receives_the_admin_digest(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """`it_admin` is an admin for every purpose, including being mailed.

    `CurrentUser._normalize_role` collapses IT_ADMIN into ADMIN, but only for the
    token-derived caller - the stored `employees.role` column keeps `it_admin`.
    Addressing the digest with `list_by_role(Role.ADMIN)` therefore queried the
    stored column and silently skipped every IT admin, so the people who run the
    workspace never got the report built for them.
    """
    it_admin = Employee(
        hr_external_id="hr-itadmin",
        work_email="itadmin@corp.test",
        full_name="Ira ITAdmin",
        role=Role.IT_ADMIN,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(it_admin)
    await db.commit()

    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)
    await service.send_digests(datetime.now(UTC), _EVENING)

    everyone = {str(m["to"]) for m in email.messages} | {
        a
        for m in email.messages
        for a in m["cc"]  # type: ignore[union-attr]
    }
    assert "itadmin@corp.test" in everyone
    assert "admin@corp.test" in everyone


async def test_a_failing_digest_send_cannot_undo_already_delivered_reports(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """The regression this guards is subtle and expensive.

    `_send` emails the manager and then only FLUSHES the SENT status; the worker
    commits once at the end of the tick and rolls back on any exception. The
    digest run happens after the individual sends in that same transaction, so an
    escaping error there would roll those rows back to DRAFT while the mail is
    already delivered - and the next tick would send every manager their report
    again.
    """

    class _BrokenDigestEmail(_CapturingEmail):
        async def send(
            self, *, to: str, subject: str, html: str, cc: list[str] | None = None
        ) -> None:
            if subject.startswith("EOD · "):  # the admins' rollup
                raise RuntimeError("smtp went away mid-digest")
            await super().send(to=to, subject=subject, html=html, cc=cc)

    await _add_department(db, "Design", "dia")
    db.add(
        EodReport(
            employee_id=seed.report.id,
            report_date=_DIGEST_DAY,
            status=EodStatus.DRAFT,
            summary="A day.",
            highlights={},
        )
    )
    await db.commit()

    email = _BrokenDigestEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)
    assert await service.auto_send_due(datetime.now(UTC), _EVENING) == 1
    assert email.sent == ["report@corp.test"]  # delivered, irreversibly

    # Must NOT raise - that is the whole point. The company rollup is the one
    # that failed; the other rollups still go out.
    await service.send_digests(datetime.now(UTC), _EVENING)
    assert not [m for m in email.messages if str(m["subject"]).startswith("EOD · ")]

    # The individual report stays SENT, so the next tick will not resend it.
    row = (
        await db.execute(select(EodReport).where(EodReport.employee_id == seed.report.id))
    ).scalar_one()
    assert row.status is EodStatus.SENT


def _rollups(email: _CapturingEmail, prefix: str, to: str) -> list[dict[str, object]]:
    return [m for m in email.messages if str(m["subject"]).startswith(prefix) and m["to"] == to]


def _sent_report(employee: Employee, summary: str, **highlights: object) -> EodReport:
    return EodReport(
        employee_id=employee.id,
        report_date=_DIGEST_DAY,
        status=EodStatus.SENT,
        summary=summary,
        highlights=highlights,
        metrics={"tasks_done": 1},
    )


async def test_everyone_gets_their_own_report_at_send_time(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """The scheduled send mails each person their own report, not their manager."""
    db.add(
        EodReport(
            employee_id=seed.report.id,
            report_date=_DIGEST_DAY,
            status=EodStatus.DRAFT,
            summary="Shipped the thing.",
            highlights={},
        )
    )
    await db.commit()
    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)

    assert await service.auto_send_due(datetime.now(UTC), _EVENING) == 1

    assert email.sent == ["report@corp.test"]
    assert str(email.messages[0]["subject"]).startswith("Your end of day · ")
    assert "Shipped the thing." in str(email.messages[0]["html"])


async def test_manager_gets_one_team_rollup_with_narratives(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """A direct manager gets ONE team rollup carrying each report's own account
    and blockers - what the personal EOD scope already lets them read - while
    the admins' company rollup carries neither."""
    db.add(
        _sent_report(
            seed.report,
            "PRIVATE-NARRATIVE: refactored the ingest path.",
            tasks_completed=["Ingest refactor"],
            blockers=["Waiting on the staging database"],
        )
    )
    await db.commit()
    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)

    await service.send_digests(datetime.now(UTC), _EVENING)

    (team,) = _rollups(email, "Team EOD · ", "manager@corp.test")
    html = str(team["html"])
    assert "Remy Report" in html
    assert "PRIVATE-NARRATIVE: refactored the ingest path." in html
    assert "Ingest refactor" in html
    assert "Waiting on the staging database" in html
    # The company rollup names the work and the blocker, never the narrative.
    company = str(_admin_digest(email)["html"])
    assert "PRIVATE-NARRATIVE" not in company
    assert "Ingest refactor" in company
    # No individual report was mailed to the manager, and no team rollup to
    # anyone without reports.
    assert "Team EOD · " not in [
        str(m["subject"]) for m in email.messages if m["to"] != "manager@corp.test"
    ]


async def test_department_head_gets_one_department_rollup_with_narratives(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """A senior manager with a department gets ONE department rollup. It
    carries the narratives of everyone in the department (that role's personal
    scope IS the department), and a direct report inside the department is not
    repeated in a separate team rollup."""
    head = await _add_person(db, "hema", Role.SENIOR_MANAGER, "Design")
    dia, devi = await _add_department(db, "Design", "dia", "devi")
    devi.manager_id = head.id
    db.add(
        _sent_report(dia, "PRIVATE-NARRATIVE: finished the icon set.", tasks_completed=["Icon set"])
    )
    db.add(_sent_report(devi, "PRIVATE-NARRATIVE: type scale review."))
    await db.commit()
    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)

    await service.send_digests(datetime.now(UTC), _EVENING)

    (department,) = _rollups(email, "Design EOD · ", "hema@corp.test")
    html = str(department["html"])
    assert "Dia" in html and "Devi" in html and "Hema" in html
    assert "finished the icon set" in html and "type scale review" in html
    assert _rollups(email, "Team EOD · ", "hema@corp.test") == []
    assert "PRIVATE-NARRATIVE" not in str(_admin_digest(email)["html"])


async def test_admin_who_manages_people_also_gets_a_team_rollup(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """The company rollup cannot carry narratives, so an admin with direct
    reports gets a team rollup as well - the only way they see what their own
    people wrote, exactly as any lead does."""
    seed.outsider.manager_id = seed.admin.id
    db.add(_sent_report(seed.outsider, "PRIVATE-NARRATIVE: closed the quarter."))
    await db.commit()
    email = _CapturingEmail()
    service = _build_service(db, settings, llm=_StubLlm(), email=email)

    await service.send_digests(datetime.now(UTC), _EVENING)

    company = _admin_digest(email)
    assert company["to"] == "admin@corp.test"
    assert "PRIVATE-NARRATIVE" not in str(company["html"])
    (team,) = _rollups(email, "Team EOD · ", "admin@corp.test")
    assert "closed the quarter" in str(team["html"])
    assert "Olive Outsider" in str(team["html"])

"""The Founder Meetings scheduler: retries, reminders, and closing out.

No network. Google is either left unconfigured - which is how a request
reaches SCHEDULING_FAILED in the first place - or stubbed to succeed, which is
how a retry recovers. The three jobs are tested apart and then together,
because the thing that matters about the tick is that one of them failing does
not take the other two with it.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.meeting_request import MeetingRequest, MeetingStatus
from app.models.notification import Notification, NotificationKind
from app.repositories.employee import EmployeeRepository
from app.repositories.meeting_request import MeetingRequestRepository
from app.repositories.meeting_settings import MeetingSettingsRepository
from app.repositories.notification import NotificationRepository
from app.services.meeting_booking_service import MeetingBookingService
from app.services.meeting_scheduler_service import MeetingSchedulerService
from app.services.notification_service import NotificationService
from tests.conftest import auth_headers
from tests.test_founder_meetings import _body, _submit, _when, booked, fm  # noqa: F401

BASE = "/api/v1/meetings/requests"


def _scheduler(settings: Settings, db: AsyncSession) -> MeetingSchedulerService:
    """Wire it exactly as `worker/meeting_scheduler.py` does."""
    requests = MeetingRequestRepository(db)
    employees = EmployeeRepository(db)
    meeting_settings = MeetingSettingsRepository(db)
    return MeetingSchedulerService(
        requests,
        meeting_settings,
        MeetingBookingService(settings, requests, employees, meeting_settings),
        NotificationService(NotificationRepository(db)),
    )


async def _row(db: AsyncSession, request_id: str) -> MeetingRequest:
    found = await MeetingRequestRepository(db).get(__import__("uuid").UUID(request_id))
    assert found is not None
    return found


async def _notifications(db: AsyncSession, kind: NotificationKind) -> list[Notification]:
    rows = await db.execute(select(Notification).where(Notification.kind == kind))
    return list(rows.scalars().all())


@pytest_asyncio.fixture
async def confirmed(
    client: AsyncClient,
    settings: Settings,
    db: AsyncSession,
    fm: SimpleNamespace,  # noqa: F811
    booked: None,  # noqa: F811
) -> SimpleNamespace:
    """A meeting on the calendar, with one other person in the room."""
    created = await _submit(
        client,
        settings,
        fm.requester,
        participant_ids=[str(fm.manager.id)],
    )
    approved = await client.post(
        f"{BASE}/{created['id']}/approve",
        json={},
        headers=auth_headers(settings, fm.office),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "confirmed"
    return SimpleNamespace(id=created["id"], fm=fm)


# --------------------------------------------------------------------------- #
# Reminders
# --------------------------------------------------------------------------- #
async def test_the_requester_and_the_room_are_reminded_but_not_the_ceo(
    settings: Settings, db: AsyncSession, confirmed: SimpleNamespace
) -> None:
    """The CEO holds the calendar invitation and Google already reminds them.
    A second nudge per meeting is how somebody turns notifications off."""
    row = await _row(db, confirmed.id)
    assert row.scheduled_start_at is not None

    # Stand just inside the lead time.
    now = row.scheduled_start_at - timedelta(minutes=5)
    outcome = await _scheduler(settings, db).run_tick(now=now)
    await db.commit()

    assert outcome.reminded == 1
    sent = await _notifications(db, NotificationKind.MEETING_REMINDER)
    recipients = {n.recipient_id for n in sent}
    assert confirmed.fm.requester.id in recipients
    assert confirmed.fm.manager.id in recipients
    assert confirmed.fm.ceo.id not in recipients


async def test_a_reminder_is_sent_once_however_often_the_tick_runs(
    settings: Settings, db: AsyncSession, confirmed: SimpleNamespace
) -> None:
    """The whole reason `reminder_sent_at` exists: the tick is minutes and the
    window is tens of minutes, so without it every sweep would nudge again."""
    row = await _row(db, confirmed.id)
    assert row.scheduled_start_at is not None
    now = row.scheduled_start_at - timedelta(minutes=5)

    first = await _scheduler(settings, db).run_tick(now=now)
    await db.commit()
    second = await _scheduler(settings, db).run_tick(now=now + timedelta(minutes=1))
    await db.commit()

    assert (first.reminded, second.reminded) == (1, 0)
    assert len(await _notifications(db, NotificationKind.MEETING_REMINDER)) == 2  # 2 people, once


async def test_a_meeting_that_already_started_is_not_announced_as_soon(
    settings: Settings, db: AsyncSession, confirmed: SimpleNamespace
) -> None:
    """The lower bound on the window. A worker that was down over the weekend
    must not come back on Monday and announce Friday's meetings as starting
    soon - the rows are still CONFIRMED, because nothing marked them complete
    while it was dead."""
    row = await _row(db, confirmed.id)
    assert row.scheduled_start_at is not None

    outcome = await _scheduler(settings, db).run_tick(
        now=row.scheduled_start_at + timedelta(minutes=1)
    )
    await db.commit()

    assert outcome.reminded == 0
    assert await _notifications(db, NotificationKind.MEETING_REMINDER) == []


async def test_a_zero_lead_time_turns_reminders_off(
    settings: Settings, db: AsyncSession, confirmed: SimpleNamespace
) -> None:
    """Zero means off, not "every confirmed meeting is due one right now"."""
    config = await MeetingSettingsRepository(db).get_or_create()
    config.reminder_lead_minutes = 0
    await db.commit()

    row = await _row(db, confirmed.id)
    assert row.scheduled_start_at is not None
    outcome = await _scheduler(settings, db).run_tick(
        now=row.scheduled_start_at - timedelta(minutes=5)
    )
    await db.commit()

    assert outcome.reminded == 0


# --------------------------------------------------------------------------- #
# Closing out
# --------------------------------------------------------------------------- #
async def test_a_meeting_whose_time_has_passed_is_completed(
    settings: Settings, db: AsyncSession, confirmed: SimpleNamespace
) -> None:
    row = await _row(db, confirmed.id)
    assert row.scheduled_end_at is not None

    outcome = await _scheduler(settings, db).run_tick(
        now=row.scheduled_end_at + timedelta(minutes=1)
    )
    await db.commit()

    assert outcome.completed == 1
    after = await _row(db, confirmed.id)
    assert after.status is MeetingStatus.COMPLETED
    assert after.completed_at is not None


async def test_completing_is_idempotent(
    settings: Settings, db: AsyncSession, confirmed: SimpleNamespace
) -> None:
    row = await _row(db, confirmed.id)
    assert row.scheduled_end_at is not None
    after_end = row.scheduled_end_at + timedelta(minutes=1)

    first = await _scheduler(settings, db).run_tick(now=after_end)
    await db.commit()
    second = await _scheduler(settings, db).run_tick(now=after_end)
    await db.commit()

    assert (first.completed, second.completed) == (1, 0)


# --------------------------------------------------------------------------- #
# Retrying a booking the calendar refused
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def failed(
    client: AsyncClient,
    settings: Settings,
    db: AsyncSession,
    fm: SimpleNamespace,  # noqa: F811
) -> SimpleNamespace:
    """An approved request Google would not take.

    No `booked` fixture, so the service account is unconfigured and the
    booking records SCHEDULING_FAILED - which is how this really happens.
    """
    created = await _submit(client, settings, fm.requester)
    approved = await client.post(
        f"{BASE}/{created['id']}/approve",
        json={},
        headers=auth_headers(settings, fm.office),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "scheduling_failed"
    return SimpleNamespace(id=created["id"], fm=fm)


async def test_a_refused_booking_is_retried_and_recovers(
    settings: Settings,
    db: AsyncSession,
    failed: SimpleNamespace,
    booked: None,  # noqa: F811
) -> None:
    """Nothing else recovers these: no invitation exists to remind anyone the
    meeting was supposed to happen."""
    outcome = await _scheduler(settings, db).run_tick()
    await db.commit()

    assert (outcome.retried, outcome.rebooked) == (1, 1)
    after = await _row(db, failed.id)
    assert after.status is MeetingStatus.CONFIRMED
    assert after.scheduled_start_at is not None


async def test_the_requester_is_told_when_a_failed_booking_finally_lands(
    settings: Settings,
    db: AsyncSession,
    failed: SimpleNamespace,
    booked: None,  # noqa: F811
) -> None:
    """They were told it failed, so they are owed the correction."""
    await _scheduler(settings, db).run_tick()
    await db.commit()

    confirmations = await _notifications(db, NotificationKind.MEETING_CONFIRMED)
    assert [n.recipient_id for n in confirmations] == [failed.fm.requester.id]


async def test_a_retry_that_fails_again_leaves_it_for_the_next_tick(
    settings: Settings, db: AsyncSession, failed: SimpleNamespace
) -> None:
    """Google is still unconfigured here. The request must stay retryable
    rather than being dropped or double-booked."""
    first = await _scheduler(settings, db).run_tick()
    await db.commit()
    assert (first.retried, first.rebooked) == (1, 0)

    after = await _row(db, failed.id)
    assert after.status is MeetingStatus.SCHEDULING_FAILED

    second = await _scheduler(settings, db).run_tick()
    await db.commit()
    assert second.retried == 1


async def test_a_retry_never_books_a_time_the_requester_has_not_accepted(
    client: AsyncClient,
    settings: Settings,
    db: AsyncSession,
    fm: SimpleNamespace,  # noqa: F811
    booked: None,  # noqa: F811
) -> None:
    """The one rule the scheduler must not break on its own.

    A request that failed, and has since had a different time proposed by the
    office, has an unaccepted approver slot on it. Retrying must not quietly
    book that - the requester never agreed to it.
    """
    created = await _submit(client, settings, fm.requester)
    row_id = created["id"]
    # Force the failed state without Google, then propose a new time.
    stuck = await _row(db, row_id)
    stuck.status = MeetingStatus.SCHEDULING_FAILED
    await db.commit()

    proposed = await client.post(
        f"{BASE}/{row_id}/suggest-slot",
        json={"slot": {"starts_at": _when(5), "duration_minutes": 30}, "note": "Try this"},
        headers=auth_headers(settings, fm.office),
    )
    assert proposed.status_code == 200, proposed.text

    # Back to failed, with an outstanding unaccepted proposal on the row.
    stuck = await _row(db, row_id)
    stuck.status = MeetingStatus.SCHEDULING_FAILED
    await db.commit()

    outcome = await _scheduler(settings, db).run_tick()
    await db.commit()

    assert outcome.rebooked == 0
    after = await _row(db, row_id)
    assert after.status is MeetingStatus.SCHEDULING_FAILED


# --------------------------------------------------------------------------- #
# The tick as a whole
# --------------------------------------------------------------------------- #
async def test_one_meeting_failing_does_not_strand_the_rest(
    client: AsyncClient,
    settings: Settings,
    db: AsyncSession,
    fm: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolation is per row, and that is the part that matters.

    One meeting whose booking blows up must not stop the next one being
    retried. Without the per-row wrapping a single poisoned request would
    stall every other meeting in the company behind it, tick after tick.
    """
    ids = []
    for index in range(2):
        created = await _submit(client, settings, fm.requester, preferred_start_at=_when(3 + index))
        approved = await client.post(
            f"{BASE}/{created['id']}/approve",
            json={},
            headers=auth_headers(settings, fm.office),
        )
        assert approved.json()["status"] == "scheduling_failed"
        ids.append(created["id"])

    seen: list[str] = []
    real_book = MeetingBookingService.book

    async def _explode_on_the_first(
        self: MeetingBookingService, request: MeetingRequest, slot: Any, **kwargs: Any
    ) -> MeetingRequest:
        seen.append(str(request.id))
        if len(seen) == 1:
            raise RuntimeError("calendar exploded")
        return await real_book(self, request, slot, **kwargs)

    monkeypatch.setattr(MeetingBookingService, "book", _explode_on_the_first)

    outcome = await _scheduler(settings, db).run_tick()
    await db.commit()

    # Both were attempted, and the failure was recorded rather than raised.
    assert len(seen) == 2
    assert outcome.retried == 2
    assert outcome.failures == ["retry:RuntimeError"]

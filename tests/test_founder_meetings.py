"""Founder Meetings - authorization, the approval state machine, and privacy.

Google is never reached: the suite either leaves the service account
unconfigured (which exercises the SCHEDULING_FAILED path) or stubs
`GoogleCalendarClient` (which exercises CONFIRMED). No network, per Testing
section 9.

The authorization block is not optional (be/CLAUDE.md section 9). It proves the
four boundaries this feature introduces: a stranger cannot read a request, an
ordinary employee cannot act as the Founder's Office, nobody but the configured
CEO can take the CEO decision, and an ADMIN - deliberately - cannot read other
people's requests at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.meeting_approval import MeetingApproval
from app.models.meeting_request import MAX_ATTACHMENTS, MeetingStatus
from app.models.meeting_settings import MeetingSettings
from app.models.notification import Notification, NotificationKind
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.meeting_request import MeetingRequestRepository
from app.repositories.meeting_settings import MeetingSettingsRepository
from app.repositories.notification import NotificationRepository
from app.services.google_calendar import GoogleCalendarClient
from app.services.meeting_booking_service import MeetingBookingService
from app.services.meeting_request_service import MeetingRequestService
from app.services.notification_service import NotificationService
from tests.conftest import _Seed, auth_headers

BASE = "/api/v1/meetings/requests"


def _when(days: float = 3) -> str:
    """A time far enough ahead to clear the 4-hour notice floor."""
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Enterprise pricing floor",
        "category": "business_strategy",
        "purpose": "We keep discounting below the floor to win logos.",
        "agenda": "1. Last quarter's discounts 2. Where the floor should sit 3. Decision",
        "expected_outcome": "A number I can hold the team to.",
        "priority": "normal",
        "duration_minutes": 30,
        "preferred_start_at": _when(),
    }
    payload.update(overrides)
    return payload


@pytest_asyncio.fixture
async def fm(db: AsyncSession, seed: _Seed) -> SimpleNamespace:
    """A configured workspace: a CEO, a Founder's Office, and a requester.

    `seed.report` is the requester, `seed.outsider` the stranger who must not be
    able to read it, and `seed.admin` the admin who deliberately cannot either.
    """
    ceo = Employee(
        hr_external_id="hr-ceo",
        work_email="ceo@corp.test",
        full_name="Cora Founder",
        role=Role.EXECUTIVE,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    office = Employee(
        hr_external_id="hr-office",
        work_email="office@corp.test",
        full_name="Omar Office",
        role=Role.EXECUTIVE,
        founders_office=True,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add_all([ceo, office])
    await db.flush()
    db.add(MeetingSettings(ceo_employee_id=ceo.id, founders_office_may_confirm=True))
    await db.commit()
    return SimpleNamespace(
        ceo=ceo,
        office=office,
        requester=seed.report,
        outsider=seed.outsider,
        admin=seed.admin,
        manager=seed.manager,
    )


async def _submit(
    client: AsyncClient, settings: Settings, who: Employee, **overrides: Any
) -> dict[str, Any]:
    resp = await client.post(BASE, json=_body(**overrides), headers=auth_headers(settings, who))
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def booked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Google appear configured and succeed, without touching the network."""
    monkeypatch.setattr(GoogleCalendarClient, "configured", property(lambda self: True))

    async def _insert(self: GoogleCalendarClient, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": "evt_abc123",
            "htmlLink": "https://calendar.google.com/event?eid=abc123",
            "iCalUID": "abc123@google.com",
            "etag": '"42"',
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
        }

    monkeypatch.setattr(GoogleCalendarClient, "insert_event", _insert)


# --------------------------------------------------------------------------- #
# Authorization (be/CLAUDE.md section 9 - not optional)
# --------------------------------------------------------------------------- #
async def test_every_endpoint_requires_auth(client: AsyncClient, fm: SimpleNamespace) -> None:
    assert (await client.get(BASE)).status_code == 401
    assert (await client.post(BASE, json=_body())).status_code == 401
    assert (await client.get(f"{BASE}/counts")).status_code == 401
    assert (await client.get("/api/v1/meetings/settings")).status_code == 401


async def test_a_stranger_cannot_read_someone_elses_request(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    """404, not 403: guessing an id must not reveal that a meeting exists."""
    created = await _submit(client, settings, fm.requester)
    resp = await client.get(f"{BASE}/{created['id']}", headers=auth_headers(settings, fm.outsider))
    assert resp.status_code == 404


async def test_an_admin_cannot_read_other_peoples_requests(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    """The deliberate privacy call, mirroring reimbursements: running the
    workspace is not a reason to read what somebody wanted to tell the founder
    privately. Admins configure the feature and see nothing else."""
    created = await _submit(client, settings, fm.requester)
    resp = await client.get(f"{BASE}/{created['id']}", headers=auth_headers(settings, fm.admin))
    assert resp.status_code == 404
    listed = await client.get(BASE, headers=auth_headers(settings, fm.admin))
    assert listed.status_code == 200
    assert listed.json()["total"] == 0


async def test_an_ordinary_employee_cannot_act_as_the_founders_office(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    created = await _submit(client, settings, fm.requester)
    headers = auth_headers(settings, fm.manager)
    for path, payload in (
        ("approve", {}),
        ("forward", {}),
        ("reject", {"note": "no capacity"}),
        ("request-info", {"note": "what is the ask"}),
        ("review", {}),
    ):
        resp = await client.post(f"{BASE}/{created['id']}/{path}", json=payload, headers=headers)
        # 404 where the manager cannot even see the row, 403 where they can.
        assert resp.status_code in (403, 404), f"{path} -> {resp.status_code}"


async def test_only_the_configured_ceo_can_take_the_ceo_decision(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    created = await _submit(client, settings, fm.requester)
    await client.post(
        f"{BASE}/{created['id']}/forward", json={}, headers=auth_headers(settings, fm.office)
    )
    # The Founder's Office is not the CEO, however much else it can do.
    resp = await client.post(
        f"{BASE}/{created['id']}/ceo-decision",
        json={"action": "approve"},
        headers=auth_headers(settings, fm.office),
    )
    assert resp.status_code == 403


async def test_a_participant_can_read_the_request_they_are_in(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    created = await _submit(client, settings, fm.requester, participant_ids=[str(fm.outsider.id)])
    resp = await client.get(f"{BASE}/{created['id']}", headers=auth_headers(settings, fm.outsider))
    assert resp.status_code == 200
    assert resp.json()["participant_count"] == 1


async def test_only_an_admin_can_change_the_settings(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    """The Founder's Office reads them but cannot appoint its own approver."""
    read = await client.get("/api/v1/meetings/settings", headers=auth_headers(settings, fm.office))
    assert read.status_code == 200
    blocked = await client.patch(
        "/api/v1/meetings/settings",
        json={"min_notice_hours": 0},
        headers=auth_headers(settings, fm.office),
    )
    assert blocked.status_code == 403
    allowed = await client.patch(
        "/api/v1/meetings/settings",
        json={"min_notice_hours": 1},
        headers=auth_headers(settings, fm.admin),
    )
    assert allowed.status_code == 200
    assert allowed.json()["min_notice_hours"] == 1


async def test_only_the_requester_can_edit_or_answer_a_proposal(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    created = await _submit(client, settings, fm.requester)
    # The office can see it, but editing somebody's request is not theirs to do.
    resp = await client.patch(
        f"{BASE}/{created['id']}",
        json={"title": "Rewritten by the office"},
        headers=auth_headers(settings, fm.office),
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Submission guards
# --------------------------------------------------------------------------- #
async def test_submission_is_refused_until_a_ceo_is_named(
    client: AsyncClient, settings: Settings, db: AsyncSession, seed: _Seed
) -> None:
    """No `fm` fixture here: the workspace is unconfigured on purpose."""
    resp = await client.post(BASE, json=_body(), headers=auth_headers(settings, seed.report))
    assert resp.status_code == 422
    assert "not set up" in resp.json()["error"]["message"]


async def test_notice_and_window_limits_are_refused_with_a_reason(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    headers = auth_headers(settings, fm.requester)
    too_soon = await client.post(
        BASE, json=_body(preferred_start_at=_when(days=0.05)), headers=headers
    )
    assert too_soon.status_code == 422
    assert "notice" in too_soon.json()["error"]["message"]

    too_far = await client.post(
        BASE, json=_body(preferred_start_at=_when(days=400)), headers=headers
    )
    assert too_far.status_code == 422
    assert "days ahead" in too_far.json()["error"]["message"]


async def test_the_ceo_cannot_request_a_meeting_with_themselves(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    resp = await client.post(BASE, json=_body(), headers=auth_headers(settings, fm.ceo))
    assert resp.status_code == 422


async def test_a_past_or_naive_time_is_refused_by_the_schema(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    headers = auth_headers(settings, fm.requester)
    naive = datetime.now().replace(microsecond=0) + timedelta(days=3)
    resp = await client.post(
        BASE, json=_body(preferred_start_at=naive.isoformat()), headers=headers
    )
    assert resp.status_code == 422
    assert "timezone offset" in resp.json()["error"]["message"]


# --------------------------------------------------------------------------- #
# The approval state machine
# --------------------------------------------------------------------------- #
async def test_the_office_can_confirm_and_book_it(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace, booked: None
) -> None:
    """The user's own requirement: the Founder's Office accepts the request."""
    created = await _submit(client, settings, fm.requester)
    resp = await client.post(
        f"{BASE}/{created['id']}/approve", json={}, headers=auth_headers(settings, fm.office)
    )
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["status"] == MeetingStatus.CONFIRMED.value
    assert detail["scheduled_start_at"] is not None
    assert detail["event"]["google_event_id"] == "evt_abc123"
    assert detail["event"]["meet_url"].startswith("https://meet.google.com/")
    # One approval, at the office's stage, and it approved rather than forwarded.
    assert [(a["stage"], a["decision"]) for a in detail["approvals"]] == [
        ("founders_office", "approved")
    ]


async def test_the_office_cannot_confirm_when_the_workspace_reserves_it(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    await client.patch(
        "/api/v1/meetings/settings",
        json={"founders_office_may_confirm": False},
        headers=auth_headers(settings, fm.admin),
    )
    created = await _submit(client, settings, fm.requester)
    resp = await client.post(
        f"{BASE}/{created['id']}/approve", json={}, headers=auth_headers(settings, fm.office)
    )
    assert resp.status_code == 422
    assert "forward it instead" in resp.json()["error"]["message"]


async def test_a_reserved_category_must_go_to_the_ceo(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    await client.patch(
        "/api/v1/meetings/settings",
        json={"categories_requiring_ceo": ["personal_confidential"]},
        headers=auth_headers(settings, fm.admin),
    )
    created = await _submit(client, settings, fm.requester, category="personal_confidential")
    resp = await client.post(
        f"{BASE}/{created['id']}/approve", json={}, headers=auth_headers(settings, fm.office)
    )
    assert resp.status_code == 422
    assert "personally" in resp.json()["error"]["message"]


async def test_forward_then_ceo_approve_books_it_and_keeps_both_decisions(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace, booked: None
) -> None:
    created = await _submit(client, settings, fm.requester)
    forwarded = await client.post(
        f"{BASE}/{created['id']}/forward",
        json={"note": "Worth your time."},
        headers=auth_headers(settings, fm.office),
    )
    assert forwarded.status_code == 200
    assert forwarded.json()["status"] == MeetingStatus.AWAITING_CEO_APPROVAL.value

    decided = await client.post(
        f"{BASE}/{created['id']}/ceo-decision",
        json={"action": "approve"},
        headers=auth_headers(settings, fm.ceo),
    )
    assert decided.status_code == 200, decided.text
    detail = decided.json()
    assert detail["status"] == MeetingStatus.CONFIRMED.value
    # The history keeps BOTH decisions, which a reviewer-column shape could not.
    assert [(a["stage"], a["decision"]) for a in detail["approvals"]] == [
        ("founders_office", "forwarded"),
        ("ceo", "approved"),
    ]


async def test_a_proposed_time_cannot_be_booked_until_the_requester_accepts(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace, booked: None
) -> None:
    """The rule the whole slot model exists for: no meeting moves silently."""
    created = await _submit(client, settings, fm.requester)
    office = auth_headers(settings, fm.office)
    proposed = await client.post(
        f"{BASE}/{created['id']}/suggest-slot",
        json={"slot": {"starts_at": _when(days=5), "duration_minutes": 15}},
        headers=office,
    )
    assert proposed.status_code == 200, proposed.text
    detail = proposed.json()
    assert detail["status"] == MeetingStatus.SLOT_PROPOSED.value
    offered = next(s for s in detail["slots"] if s["origin"] == "founders_office")

    # Trying to book the offered time before the requester has agreed.
    jumped = await client.post(
        f"{BASE}/{created['id']}/approve",
        json={"slot_id": offered["id"]},
        headers=office,
    )
    assert jumped.status_code == 422
    assert "not accepted that time" in jumped.json()["error"]["message"]

    accepted = await client.post(
        f"{BASE}/{created['id']}/accept-slot",
        json={"accept": True},
        headers=auth_headers(settings, fm.requester),
    )
    assert accepted.status_code == 200, accepted.text
    # The office may confirm in this workspace, so acceptance books it outright
    # rather than making the requester wait for a second click.
    assert accepted.json()["status"] == MeetingStatus.CONFIRMED.value


async def test_refusing_a_proposed_time_returns_it_to_the_office(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    created = await _submit(client, settings, fm.requester)
    await client.post(
        f"{BASE}/{created['id']}/suggest-slot",
        json={"slot": {"starts_at": _when(days=5), "duration_minutes": 30}},
        headers=auth_headers(settings, fm.office),
    )
    refused = await client.post(
        f"{BASE}/{created['id']}/accept-slot",
        json={"accept": False, "note": "I am travelling that week."},
        headers=auth_headers(settings, fm.requester),
    )
    assert refused.status_code == 200
    assert refused.json()["status"] == MeetingStatus.UNDER_REVIEW.value
    assert any(s["status"] == "declined" for s in refused.json()["slots"])


async def test_a_send_back_is_answered_by_editing_and_clears_the_question(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    created = await _submit(client, settings, fm.requester)
    sent_back = await client.post(
        f"{BASE}/{created['id']}/request-info",
        json={"note": "Which client is escalating?"},
        headers=auth_headers(settings, fm.office),
    )
    assert sent_back.status_code == 200
    assert sent_back.json()["status"] == MeetingStatus.NEEDS_INFORMATION.value
    assert sent_back.json()["info_request_note"] == "Which client is escalating?"

    answered = await client.patch(
        f"{BASE}/{created['id']}",
        json={"agenda": "1. Northwind is threatening to leave 2. What we offer 3. Decision"},
        headers=auth_headers(settings, fm.requester),
    )
    assert answered.status_code == 200
    assert answered.json()["status"] == MeetingStatus.SUBMITTED.value
    assert answered.json()["info_request_note"] is None


async def test_rejection_requires_a_reason_and_closes_the_request(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    created = await _submit(client, settings, fm.requester)
    office = auth_headers(settings, fm.office)
    assert (
        await client.post(f"{BASE}/{created['id']}/reject", json={}, headers=office)
    ).status_code == 422
    rejected = await client.post(
        f"{BASE}/{created['id']}/reject",
        json={"note": "Take this to the COO first."},
        headers=office,
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == MeetingStatus.REJECTED.value
    assert rejected.json()["can_decide"] is False


async def test_the_requester_cannot_cancel_a_confirmed_meeting_themselves(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace, booked: None
) -> None:
    """They ask the office instead, so nobody vanishes from a calendar
    unannounced."""
    created = await _submit(client, settings, fm.requester)
    await client.post(
        f"{BASE}/{created['id']}/approve", json={}, headers=auth_headers(settings, fm.office)
    )
    resp = await client.post(
        f"{BASE}/{created['id']}/cancel",
        json={"reason": "Sorted over email."},
        headers=auth_headers(settings, fm.requester),
    )
    assert resp.status_code == 422
    assert "Founder's Office" in resp.json()["error"]["message"]


async def test_the_requester_can_withdraw_before_it_is_scheduled(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    created = await _submit(client, settings, fm.requester)
    resp = await client.post(
        f"{BASE}/{created['id']}/cancel",
        json={"reason": "Answered in standup."},
        headers=auth_headers(settings, fm.requester),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == MeetingStatus.CANCELLED.value


# --------------------------------------------------------------------------- #
# Booking failure must not discard the approval
# --------------------------------------------------------------------------- #
async def test_a_calendar_failure_keeps_the_decision_and_is_retryable(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace, db: AsyncSession
) -> None:
    """No `booked` fixture: the service account is unconfigured, so the booking
    cannot succeed. Both approvers' judgement is real work and must survive it.
    """
    created = await _submit(client, settings, fm.requester)
    resp = await client.post(
        f"{BASE}/{created['id']}/approve", json={}, headers=auth_headers(settings, fm.office)
    )
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["status"] == MeetingStatus.SCHEDULING_FAILED.value
    assert detail["event"]["sync_status"] == "failed"
    assert "not configured" in detail["event"]["last_error"]
    assert detail["event"]["attempt_count"] == 1
    # The approval is still on the record, so this is a retry and not a re-decision.
    assert [(a["stage"], a["decision"]) for a in detail["approvals"]] == [
        ("founders_office", "approved")
    ]
    stored = (
        (
            await db.execute(
                select(MeetingApproval).where(
                    MeetingApproval.meeting_request_id == uuid.UUID(detail["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(stored) == 1
    # And the office can still act on it (SCHEDULING_FAILED stays actionable).
    assert detail["can_decide"] is True


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #
async def test_a_confidential_title_never_reaches_a_notification(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace, db: AsyncSession
) -> None:
    """The category exists so the subject stays between the requester and the
    people deciding - not on an office phone's lock screen."""
    secret = "Resignation and handover"
    await _submit(
        client,
        settings,
        fm.requester,
        title=secret,
        category="personal_confidential",
    )
    rows = (
        (
            await db.execute(
                select(Notification).where(Notification.kind == NotificationKind.MEETING_REQUEST)
            )
        )
        .scalars()
        .all()
    )
    assert rows, "the Founder's Office should have been notified"
    for row in rows:
        assert secret not in row.title
        assert secret not in (row.body or "")
        assert "confidential request" in row.title


async def test_an_ordinary_title_does_reach_the_office(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace, db: AsyncSession
) -> None:
    """The counterpart, so the test above is proving the category and not just
    that titles never appear."""
    await _submit(client, settings, fm.requester, title="Enterprise pricing floor")
    rows = (
        (
            await db.execute(
                select(Notification).where(Notification.kind == NotificationKind.MEETING_REQUEST)
            )
        )
        .scalars()
        .all()
    )
    assert any("Enterprise pricing floor" in row.title for row in rows)


# --------------------------------------------------------------------------- #
# Queues
# --------------------------------------------------------------------------- #
async def test_the_counts_endpoint_answers_each_queue(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    first = await _submit(client, settings, fm.requester)
    await _submit(client, settings, fm.requester, title="Second ask")
    await client.post(
        f"{BASE}/{first['id']}/forward", json={}, headers=auth_headers(settings, fm.office)
    )

    office = (await client.get(f"{BASE}/counts", headers=auth_headers(settings, fm.office))).json()
    assert office["awaiting_founders_office"] == 1
    assert office["awaiting_ceo"] == 1

    mine = (await client.get(f"{BASE}/counts", headers=auth_headers(settings, fm.requester))).json()
    assert mine["my_open_requests"] == 2

    # The office sees everything, so "mine" has to narrow to their own.
    theirs = await client.get(f"{BASE}?mine=true", headers=auth_headers(settings, fm.office))
    assert theirs.json()["total"] == 0


# --------------------------------------------------------------------------- #
# Regressions - each of these failed before the fix it is named for
# --------------------------------------------------------------------------- #
async def test_bug_ceo_approval_books_the_accepted_slot_not_the_original(
    client: AsyncClient, settings: Settings, fm: Any, booked: None
) -> None:
    """BUG 1: the office proposes a new time, the requester accepts it, the CEO
    approves - and the ORIGINAL requested time gets booked."""
    await client.patch(
        "/api/v1/meetings/settings",
        json={"founders_office_may_confirm": False},
        headers=auth_headers(settings, fm.admin),
    )
    created = await _submit(client, settings, fm.requester)
    original = created["preferred_start_at"]

    proposed = await client.post(
        f"{BASE}/{created['id']}/suggest-slot",
        json={"slot": {"starts_at": _when(days=9), "duration_minutes": 15}},
        headers=auth_headers(settings, fm.office),
    )
    assert proposed.status_code == 200, proposed.text
    offered = next(s for s in proposed.json()["slots"] if s["origin"] == "founders_office")

    accepted = await client.post(
        f"{BASE}/{created['id']}/accept-slot",
        json={"accept": True},
        headers=auth_headers(settings, fm.requester),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == MeetingStatus.AWAITING_CEO_APPROVAL.value

    decided = await client.post(
        f"{BASE}/{created['id']}/ceo-decision",
        json={"action": "approve"},
        headers=auth_headers(settings, fm.ceo),
    )
    assert decided.status_code == 200, decided.text
    detail = decided.json()
    assert detail["status"] == MeetingStatus.CONFIRMED.value
    # The agreed time, not the one the requester originally asked for.
    assert detail["scheduled_start_at"][:16] == offered["starts_at"][:16], (
        f"booked {detail['scheduled_start_at']} but the agreed slot was "
        f"{offered['starts_at']} (original ask was {original})"
    )
    assert detail["duration_minutes"] == 30  # the request's own duration is untouched


async def test_bug_editing_the_time_moves_the_slot_too(
    client: AsyncClient, settings: Settings, fm: Any, booked: None
) -> None:
    """BUG 2: the office asks for a different week, the requester edits the
    time - and approving books the time they edited AWAY from."""
    created = await _submit(client, settings, fm.requester)
    await client.post(
        f"{BASE}/{created['id']}/request-info",
        json={"note": "Can you do the following week?"},
        headers=auth_headers(settings, fm.office),
    )
    moved = _when(days=12)
    edited = await client.patch(
        f"{BASE}/{created['id']}",
        json={"preferred_start_at": moved, "duration_minutes": 45},
        headers=auth_headers(settings, fm.requester),
    )
    assert edited.status_code == 200, edited.text
    preferred = next(s for s in edited.json()["slots"] if s["origin"] == "requester_preferred")
    assert preferred["starts_at"][:16] == moved[:16], (
        f"the preferred slot still says {preferred['starts_at']} after the request moved to {moved}"
    )
    assert preferred["duration_minutes"] == 45

    booked_resp = await client.post(
        f"{BASE}/{created['id']}/approve", json={}, headers=auth_headers(settings, fm.office)
    )
    assert booked_resp.status_code == 200, booked_resp.text
    assert booked_resp.json()["scheduled_start_at"][:16] == moved[:16]


async def test_bug_a_confirmed_meeting_always_names_who_approved_it(
    client: AsyncClient, settings: Settings, fm: Any, booked: None
) -> None:
    """BUG 3: booking via slot-acceptance records no APPROVED decision, so the
    history cannot say who approved - the one thing the table exists for."""
    created = await _submit(client, settings, fm.requester)
    await client.post(
        f"{BASE}/{created['id']}/suggest-slot",
        json={"slot": {"starts_at": _when(days=6), "duration_minutes": 30}},
        headers=auth_headers(settings, fm.office),
    )
    accepted = await client.post(
        f"{BASE}/{created['id']}/accept-slot",
        json={"accept": True},
        headers=auth_headers(settings, fm.requester),
    )
    assert accepted.status_code == 200, accepted.text
    detail = accepted.json()
    assert detail["status"] == MeetingStatus.CONFIRMED.value
    approvals = [(a["stage"], a["decision"], a["decided_by_id"]) for a in detail["approvals"]]
    assert any(d == "approved" for _, d, _ in approvals), (
        f"confirmed with no APPROVED decision on record: {approvals}"
    )
    # And it is attributed to the office member who proposed it, not the
    # requester who merely said yes.
    approver = next(by for _, d, by in approvals if d == "approved")
    assert approver == str(fm.office.id), f"approval attributed to {approver}"


async def test_bug_forward_is_never_attributed_to_the_requester(
    client: AsyncClient, settings: Settings, fm: Any
) -> None:
    """BUG 4: when the office may not confirm, accepting a proposed time
    forwards the request - recorded as the REQUESTER having forwarded it."""
    await client.patch(
        "/api/v1/meetings/settings",
        json={"founders_office_may_confirm": False},
        headers=auth_headers(settings, fm.admin),
    )
    created = await _submit(client, settings, fm.requester)
    await client.post(
        f"{BASE}/{created['id']}/suggest-slot",
        json={"slot": {"starts_at": _when(days=7), "duration_minutes": 30}},
        headers=auth_headers(settings, fm.office),
    )
    accepted = await client.post(
        f"{BASE}/{created['id']}/accept-slot",
        json={"accept": True},
        headers=auth_headers(settings, fm.requester),
    )
    assert accepted.status_code == 200, accepted.text
    forwarded = [a for a in accepted.json()["approvals"] if a["decision"] == "forwarded"]
    assert forwarded, "expected a forwarded decision"
    assert forwarded[0]["decided_by_id"] != str(fm.requester.id), (
        "the requester was recorded as having forwarded their own request"
    )
    assert forwarded[0]["decided_by_id"] == str(fm.office.id)


def _service(settings: Settings, db: AsyncSession) -> MeetingRequestService:
    """The service wired to the test session, for the few rules that are
    clearer asserted directly than driven through HTTP."""
    requests = MeetingRequestRepository(db)
    employees = EmployeeRepository(db)
    meeting_settings = MeetingSettingsRepository(db)
    return MeetingRequestService(
        settings,
        requests,
        employees,
        meeting_settings,
        MeetingBookingService(settings, requests, employees, meeting_settings),
        NotificationService(NotificationRepository(db)),
        AuditRepository(db),
    )


async def test_bug_today_means_the_workspaces_day_not_utcs(
    settings: Settings, db: AsyncSession, fm: SimpleNamespace
) -> None:
    """BUG 5: "confirmed today" counted from UTC midnight, which is 05:30 in
    Asia/Kolkata - so it reported yesterday's meetings every morning until
    half five."""
    start, end = await _service(settings, db)._today_bounds()
    assert end - start == timedelta(days=1)
    # Asia/Kolkata is UTC+5:30, so its midnight is 18:30 UTC the day before.
    assert (start.hour, start.minute) == (18, 30), f"day started at {start}"


async def test_bug_booking_takes_the_other_offers_off_the_table(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace, booked: None
) -> None:
    """BUG 6: a confirmed meeting still showed an untouched alternative as
    `open`, which reads as a time somebody might still take."""
    created = await _submit(
        client,
        settings,
        fm.requester,
        alternative_slots=[{"starts_at": _when(days=8), "duration_minutes": 30}],
    )
    assert len([s for s in created["slots"] if s["status"] == "open"]) == 2
    confirmed = await client.post(
        f"{BASE}/{created['id']}/approve", json={}, headers=auth_headers(settings, fm.office)
    )
    assert confirmed.status_code == 200, confirmed.text
    slots = confirmed.json()["slots"]
    assert [s["status"] for s in slots if s["origin"] == "requester_preferred"] == ["accepted"]
    assert [s["status"] for s in slots if s["origin"] == "requester_alternative"] == ["superseded"]


async def test_bug_cancelling_survives_a_change_of_ceo(
    client: AsyncClient,
    settings: Settings,
    fm: SimpleNamespace,
    db: AsyncSession,
    booked: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG 7: the event was deleted as whoever is CEO *now*, on a calendar
    recorded as the relative "primary". Replace the CEO and the cancellation
    became impossible - the new CEO has no rights on the old one's calendar.
    """
    deletions: list[dict[str, str]] = []

    async def _delete(self: GoogleCalendarClient, **kwargs: str) -> None:
        deletions.append(dict(kwargs))

    monkeypatch.setattr(GoogleCalendarClient, "delete_event", _delete)

    created = await _submit(client, settings, fm.requester)
    await client.post(
        f"{BASE}/{created['id']}/approve", json={}, headers=auth_headers(settings, fm.office)
    )

    # A new CEO takes over after the meeting is already on the old one's diary.
    successor = Employee(
        hr_external_id="hr-ceo2",
        work_email="newceo@corp.test",
        full_name="Nadia Next",
        role=Role.EXECUTIVE,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(successor)
    await db.commit()
    swapped = await client.patch(
        "/api/v1/meetings/settings",
        json={"ceo_employee_id": str(successor.id)},
        headers=auth_headers(settings, fm.admin),
    )
    assert swapped.status_code == 200, swapped.text

    cancelled = await client.post(
        f"{BASE}/{created['id']}/cancel",
        json={"reason": "No longer needed."},
        headers=auth_headers(settings, fm.office),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == MeetingStatus.CANCELLED.value
    assert deletions, "the calendar event should have been deleted"
    # Deleted as the calendar's OWNER, and from a real address rather than
    # "primary", which would have resolved to the wrong person's diary.
    assert deletions[0]["subject"] == "ceo@corp.test"
    assert deletions[0]["calendar_id"] == "ceo@corp.test"


async def test_editing_the_room_replaces_it_rather_than_merging(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    """A replace, not a merge - a merge would make removing somebody
    impossible, and the edit form sends the list it wants."""
    created = await _submit(client, settings, fm.requester, participant_ids=[str(fm.outsider.id)])
    assert created["participant_count"] == 1
    emptied = await client.patch(
        f"{BASE}/{created['id']}",
        json={"participant_ids": []},
        headers=auth_headers(settings, fm.requester),
    )
    assert emptied.status_code == 200, emptied.text
    assert emptied.json()["participant_count"] == 0
    # And the person removed loses their read on it.
    assert (
        await client.get(f"{BASE}/{created['id']}", headers=auth_headers(settings, fm.outsider))
    ).status_code == 404


async def test_an_unknown_participant_is_refused_not_dropped(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    """Validated against the live roster, so a stale id from an old request is
    a 422 rather than a quietly smaller room."""
    resp = await client.post(
        BASE,
        json=_body(participant_ids=[str(uuid.uuid4())]),
        headers=auth_headers(settings, fm.requester),
    )
    assert resp.status_code == 422


async def test_the_requester_and_ceo_are_never_duplicated_into_the_room(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    """They are already in the meeting; naming them is a no-op rather than an
    error, because failing the whole submission over it would be pedantry."""
    created = await _submit(
        client,
        settings,
        fm.requester,
        participant_ids=[str(fm.requester.id), str(fm.ceo.id), str(fm.outsider.id)],
    )
    assert created["participant_count"] == 1
    assert [p["employee_id"] for p in created["participants"]] == [str(fm.outsider.id)]


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #
_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _upload(data: bytes, name: str = "deck.pdf", kind: str = "application/pdf") -> dict[str, Any]:
    return {"files": {"file": (name, data, kind)}}


async def test_an_attachment_round_trips_on_your_own_request(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    created = await _submit(client, settings, fm.requester)
    headers = auth_headers(settings, fm.requester)
    added = await client.post(
        f"{BASE}/{created['id']}/attachments",
        data={"label": "Pricing deck"},
        headers=headers,
        **_upload(_PDF),
    )
    assert added.status_code == 201, added.text
    assert added.json()["attachment_count"] == 1
    attachment = added.json()["attachments"][0]
    assert attachment["label"] == "Pricing deck"
    assert attachment["content_type"] == "application/pdf"
    assert attachment["size_bytes"] == len(_PDF)
    # No bytes and no storage key are ever handed out inline.
    assert "content" not in attachment
    assert "object_key" not in attachment

    got = await client.get(
        f"{BASE}/{created['id']}/attachments/{attachment['id']}", headers=headers
    )
    assert got.status_code == 200
    assert got.content == _PDF
    # Never served with a type a browser will execute.
    assert got.headers["content-type"] == "application/octet-stream"
    assert "attachment;" in got.headers["content-disposition"]

    removed = await client.delete(
        f"{BASE}/{created['id']}/attachments/{attachment['id']}", headers=headers
    )
    assert removed.status_code == 200
    assert removed.json()["attachment_count"] == 0


async def test_the_label_falls_back_to_the_filename(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    """A document is never nameless in the reviewer's list."""
    created = await _submit(client, settings, fm.requester)
    added = await client.post(
        f"{BASE}/{created['id']}/attachments",
        headers=auth_headers(settings, fm.requester),
        **_upload(_PNG, name="whiteboard.png", kind="image/png"),
    )
    assert added.status_code == 201, added.text
    assert added.json()["attachments"][0]["label"] == "whiteboard.png"


async def test_the_type_is_read_from_the_bytes_not_the_header(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    """Anything can CALL itself a PDF, and real PDFs often arrive as
    octet-stream, so the header is a claim and the magic bytes are the fact."""
    created = await _submit(client, settings, fm.requester)
    headers = auth_headers(settings, fm.requester)

    lying = await client.post(
        f"{BASE}/{created['id']}/attachments",
        headers=headers,
        **_upload(b"#!/bin/sh\nrm -rf /\n", name="deck.pdf", kind="application/pdf"),
    )
    assert lying.status_code == 422
    assert "PDF, PNG, JPG or WebP" in lying.json()["error"]["message"]

    honest = await client.post(
        f"{BASE}/{created['id']}/attachments",
        headers=headers,
        **_upload(_PDF, name="deck.pdf", kind="application/octet-stream"),
    )
    assert honest.status_code == 201, honest.text
    assert honest.json()["attachments"][0]["content_type"] == "application/pdf"


async def test_an_empty_file_and_a_sixth_file_are_both_refused(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    created = await _submit(client, settings, fm.requester)
    headers = auth_headers(settings, fm.requester)
    empty = await client.post(
        f"{BASE}/{created['id']}/attachments", headers=headers, **_upload(b"")
    )
    assert empty.status_code == 422

    for index in range(MAX_ATTACHMENTS):
        ok = await client.post(
            f"{BASE}/{created['id']}/attachments",
            headers=headers,
            **_upload(_PDF, name=f"paper{index}.pdf"),
        )
        assert ok.status_code == 201, ok.text
    sixth = await client.post(
        f"{BASE}/{created['id']}/attachments", headers=headers, **_upload(_PDF)
    )
    assert sixth.status_code == 422
    assert "at most" in sixth.json()["error"]["message"]


async def test_nobody_else_can_attach_to_or_read_your_papers(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    created = await _submit(client, settings, fm.requester)
    added = await client.post(
        f"{BASE}/{created['id']}/attachments",
        headers=auth_headers(settings, fm.requester),
        **_upload(_PDF),
    )
    attachment_id = added.json()["attachments"][0]["id"]

    # The office must be able to READ what it is deciding on...
    assert (
        await client.get(
            f"{BASE}/{created['id']}/attachments/{attachment_id}",
            headers=auth_headers(settings, fm.office),
        )
    ).status_code == 200
    # ...but not add to or remove from somebody else's request.
    assert (
        await client.post(
            f"{BASE}/{created['id']}/attachments",
            headers=auth_headers(settings, fm.office),
            **_upload(_PDF),
        )
    ).status_code == 403
    assert (
        await client.delete(
            f"{BASE}/{created['id']}/attachments/{attachment_id}",
            headers=auth_headers(settings, fm.office),
        )
    ).status_code == 403
    # A stranger sees nothing at all.
    assert (
        await client.get(
            f"{BASE}/{created['id']}/attachments/{attachment_id}",
            headers=auth_headers(settings, fm.outsider),
        )
    ).status_code == 404


async def test_papers_freeze_once_the_meeting_is_booked(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace, booked: None
) -> None:
    """An approver who has read a deck must not have a different one swapped
    under them, and a confirmed meeting's papers are part of what was agreed."""
    created = await _submit(client, settings, fm.requester)
    headers = auth_headers(settings, fm.requester)
    added = await client.post(
        f"{BASE}/{created['id']}/attachments", headers=headers, **_upload(_PDF)
    )
    attachment_id = added.json()["attachments"][0]["id"]
    await client.post(
        f"{BASE}/{created['id']}/approve", json={}, headers=auth_headers(settings, fm.office)
    )

    blocked = await client.post(
        f"{BASE}/{created['id']}/attachments", headers=headers, **_upload(_PDF)
    )
    assert blocked.status_code == 422
    assert "no longer be changed" in blocked.json()["error"]["message"]
    assert (
        await client.delete(f"{BASE}/{created['id']}/attachments/{attachment_id}", headers=headers)
    ).status_code == 422
    # Still readable, though - it is part of the record now.
    assert (
        await client.get(f"{BASE}/{created['id']}/attachments/{attachment_id}", headers=headers)
    ).status_code == 200


async def test_an_attachment_id_from_another_request_does_not_resolve(
    client: AsyncClient, settings: Settings, fm: SimpleNamespace
) -> None:
    """The request is what was scope-checked, so a file id must be checked
    against it rather than looked up globally."""
    mine = await _submit(client, settings, fm.requester)
    other = await _submit(client, settings, fm.requester, title="A separate ask")
    headers = auth_headers(settings, fm.requester)
    added = await client.post(f"{BASE}/{mine['id']}/attachments", headers=headers, **_upload(_PDF))
    attachment_id = added.json()["attachments"][0]["id"]
    crossed = await client.get(f"{BASE}/{other['id']}/attachments/{attachment_id}", headers=headers)
    assert crossed.status_code == 404


async def test_the_confirmation_says_when_in_the_requesters_own_zone(
    client: AsyncClient,
    settings: Settings,
    fm: SimpleNamespace,
    db: AsyncSession,
    booked: None,
) -> None:
    """A notification that renders an 11:00 meeting as 05:30 UTC is worse than
    one that gives no time at all."""
    created = await _submit(client, settings, fm.requester)
    confirmed = await client.post(
        f"{BASE}/{created['id']}/approve", json={}, headers=auth_headers(settings, fm.office)
    )
    assert confirmed.status_code == 200, confirmed.text
    starts = datetime.fromisoformat(confirmed.json()["scheduled_start_at"])
    local = starts.astimezone(ZoneInfo("Asia/Kolkata"))

    rows = (
        (
            await db.execute(
                select(Notification).where(Notification.kind == NotificationKind.MEETING_CONFIRMED)
            )
        )
        .scalars()
        .all()
    )
    assert rows, "the requester should have been told"
    assert all(f"{local:%H:%M}" in (row.body or "") for row in rows)
    assert all(str(local.day) in (row.body or "") for row in rows)

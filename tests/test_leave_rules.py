"""Leave rules enforced on the server: eligibility, balance and overlap (422),
cancelling approved leave, the per-request `can_decide` / `can_cancel` flags the
UI renders from, and the HR fallback for an inactive manager."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.leave import HalfDayPeriod, Leave, LeaveStatus, LeaveType
from app.models.notification import Notification, NotificationKind
from app.models.payslip import Payslip
from app.repositories.leave import LeaveRepository
from app.services.leave_service import PROBATION_HALF_DAYS_PER_MONTH, _leave_year_window
from tests.conftest import _Seed, auth_headers

# Planned leave needs 2 days' notice by default; start comfortably after that.
_LEAD_DAYS = 7


def _next_weekday(weekday: int, *, after_days: int = _LEAD_DAYS) -> date:
    """The first date at least `after_days` from today falling on `weekday`
    (0 = Monday), so a single-day leave is always one working day."""
    day = datetime.now(UTC).date() + timedelta(days=after_days)
    return day + timedelta(days=(weekday - day.weekday()) % 7)


def _at(day: date) -> str:
    return f"{day.isoformat()}T00:00:00+00:00"


def _body(
    leave_type: str, start: date, end: date | None = None, half: str | None = None
) -> dict[str, object]:
    return {
        "leave_type": leave_type,
        "start_date": _at(start),
        "end_date": _at(end or start),
        "half_day_period": half,
        "reason": "x",
    }


async def _hr(db: AsyncSession) -> Employee:
    hr = Employee(
        hr_external_id="hr-person",
        work_email="hr.person@corp.test",
        full_name="Hana HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    return hr


async def _apply(client: AsyncClient, settings: Settings, actor: Employee, body: dict[str, object]):
    return await client.post("/api/v1/leaves", json=body, headers=auth_headers(settings, actor))


async def _approved_leave(db: AsyncSession, employee: Employee, start: date, end: date) -> Leave:
    """An approved leave written straight to the table - used for leave that has
    already started, which the API (rightly) will not let anyone file."""
    leave = Leave(
        employee_id=employee.id,
        leave_type=LeaveType.PLANNED,
        start_date=datetime(start.year, start.month, start.day, tzinfo=UTC),
        end_date=datetime(end.year, end.month, end.day, tzinfo=UTC),
        status=LeaveStatus.APPROVED,
        reviewer_id=employee.manager_id,
    )
    db.add(leave)
    await db.commit()
    return leave


def _row(listing: dict[str, object], leave_id: object) -> dict[str, object]:
    items = listing["items"]
    assert isinstance(items, list)
    return next(r for r in items if r["id"] == leave_id)


# --------------------------------------------------------------------------- #
# can_decide - the UI shows Approve/Reject from this, never from the role.
# --------------------------------------------------------------------------- #
async def test_can_decide_mirrors_the_server_rule(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    resp = await _apply(client, settings, seed.report, _body("planned", _next_weekday(0)))
    assert resp.status_code == 201, resp.text
    leave_id = resp.json()["id"]

    def listed(actor: Employee):
        return client.get("/api/v1/leaves", headers=auth_headers(settings, actor))

    assert _row((await listed(seed.manager)).json(), leave_id)["can_decide"] is True
    assert _row((await listed(hr)).json(), leave_id)["can_decide"] is True
    assert _row((await listed(seed.report)).json(), leave_id)["can_decide"] is False
    # Admin who is not the manager cannot see (so cannot decide) it at all.
    admin_items = (await listed(seed.admin)).json()["items"]
    assert leave_id not in [r["id"] for r in admin_items]


async def test_hr_decides_a_managerless_leave_and_sees_the_flag(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    resp = await _apply(client, settings, seed.outsider, _body("planned", _next_weekday(0)))
    leave_id = resp.json()["id"]

    listed = await client.get("/api/v1/leaves", headers=auth_headers(settings, hr))
    assert _row(listed.json(), leave_id)["can_decide"] is True
    decided = await client.post(
        f"/api/v1/leaves/{leave_id}/decision",
        json={"approve": True},
        headers=auth_headers(settings, hr),
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["can_decide"] is False


async def test_an_out_of_scope_caller_cannot_decide(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await _apply(client, settings, seed.report, _body("planned", _next_weekday(0)))
    denied = await client.post(
        f"/api/v1/leaves/{resp.json()['id']}/decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.outsider),
    )
    assert denied.status_code == 404


# --------------------------------------------------------------------------- #
# Eligibility, balance and overlap - all 422 with a readable message.
# --------------------------------------------------------------------------- #
async def test_a_type_not_yet_earned_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """On probation the tenure band grants no planned leave - the same rule the
    balance panel shows as `eligible: false`."""
    seed.report.hire_date = datetime.now(UTC).date() - timedelta(days=10)
    await db.commit()

    resp = await _apply(client, settings, seed.report, _body("planned", _next_weekday(0)))

    assert resp.status_code == 422, resp.text
    assert "probation" in resp.json()["error"]["message"].lower()


async def test_half_day_is_not_withheld_during_probation(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Half-day only spends from planned; the policy does not lock it during
    probation, so the planned lock must not leak onto it."""
    seed.report.hire_date = datetime.now(UTC).date() - timedelta(days=10)
    await db.commit()

    resp = await _apply(
        client, settings, seed.report, _body("half_day", _next_weekday(0), half="first_half")
    )

    assert resp.status_code == 201, resp.text


async def test_probation_half_days_are_capped_per_month(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Planned leave is locked during probation, so half days are not measured
    against it; they stop at `PROBATION_HALF_DAYS_PER_MONTH` per month instead."""
    seed.report.hire_date = datetime.now(UTC).date() - timedelta(days=10)
    await db.commit()
    today = datetime.now(UTC).date()
    month = (today.replace(day=1) + timedelta(days=64)).replace(day=1)
    weekdays = [
        month + timedelta(days=offset)
        for offset in range(10)
        if (month + timedelta(days=offset)).weekday() < 5
    ]

    for day in weekdays[:PROBATION_HALF_DAYS_PER_MONTH]:
        ok = await _apply(client, settings, seed.report, _body("half_day", day, half="first_half"))
        assert ok.status_code == 201, ok.text
    refused = await _apply(
        client,
        settings,
        seed.report,
        _body("half_day", weekdays[PROBATION_HALF_DAYS_PER_MONTH], half="first_half"),
    )
    next_month = (month + timedelta(days=32)).replace(day=1)
    next_weekday = next_month + timedelta(days=(7 - next_month.weekday()) % 7)
    elsewhere = await _apply(
        client, settings, seed.report, _body("half_day", next_weekday, half="first_half")
    )

    assert refused.status_code == 422, refused.text
    assert "probation" in refused.json()["error"]["message"].lower()
    assert elsewhere.status_code == 201, elsewhere.text


async def test_approval_rechecks_the_probation_half_day_cap(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Pending requests can outnumber the cap (e.g. filed before it existed);
    approval stops at the cap and speaks to the approver."""
    seed.report.hire_date = datetime.now(UTC).date() - timedelta(days=10)
    await db.commit()
    month = (datetime.now(UTC).date().replace(day=1) + timedelta(days=64)).replace(day=1)
    weekdays = [
        month + timedelta(days=offset)
        for offset in range(10)
        if (month + timedelta(days=offset)).weekday() < 5
    ]
    pending = []
    for day in weekdays[: PROBATION_HALF_DAYS_PER_MONTH + 1]:
        leave = await _pending_leave(db, seed.report, day)
        leave.leave_type = LeaveType.HALF_DAY
        leave.half_day_period = HalfDayPeriod.FIRST_HALF
        pending.append(leave)
    await db.commit()
    mgr = auth_headers(settings, seed.manager)

    statuses = [
        (
            await client.post(
                f"/api/v1/leaves/{leave.id}/decision", json={"approve": True}, headers=mgr
            )
        )
        for leave in pending
    ]

    assert [r.status_code for r in statuses[:-1]] == [200] * PROBATION_HALF_DAYS_PER_MONTH
    assert statuses[-1].status_code == 422, statuses[-1].text
    message = statuses[-1].json()["error"]["message"]
    assert message.startswith("Remy Report is on probation")


async def test_a_request_over_the_balance_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    start = _next_weekday(0)
    # Two full working weeks: 10 working days against the default 8 planned.
    resp = await _apply(
        client, settings, seed.report, _body("planned", start, start + timedelta(days=11))
    )

    assert resp.status_code == 422, resp.text
    assert "not enough" in resp.json()["error"]["message"].lower()


async def test_unpaid_leave_is_never_capped(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    start = _next_weekday(0)
    resp = await _apply(
        client, settings, seed.report, _body("unpaid", start, start + timedelta(days=25))
    )
    assert resp.status_code == 201, resp.text


async def test_pending_requests_reserve_the_balance(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await client.put(
        "/api/v1/leaves/policy",
        json={"annual_planned_days": 1},
        headers=auth_headers(settings, seed.admin),
    )
    first = await _apply(client, settings, seed.report, _body("planned", _next_weekday(0)))
    assert first.status_code == 201, first.text
    # Half days count 0.5: nothing is left once the full day is pending.
    second = await _apply(
        client, settings, seed.report, _body("half_day", _next_weekday(1), half="first_half")
    )
    assert second.status_code == 422, second.text


async def test_approval_rechecks_the_balance(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Balances move between apply and approve (here HR lowered the quota), so
    approving re-checks against what is left after approved leave."""
    admin = auth_headers(settings, seed.admin)
    await client.put("/api/v1/leaves/policy", json={"annual_planned_days": 2}, headers=admin)
    first = await _apply(client, settings, seed.report, _body("planned", _next_weekday(0)))
    second = await _apply(client, settings, seed.report, _body("planned", _next_weekday(1)))
    assert first.status_code == second.status_code == 201
    await client.put("/api/v1/leaves/policy", json={"annual_planned_days": 1}, headers=admin)

    manager = auth_headers(settings, seed.manager)
    ok = await client.post(
        f"/api/v1/leaves/{first.json()['id']}/decision", json={"approve": True}, headers=manager
    )
    assert ok.status_code == 200, ok.text
    over = await client.post(
        f"/api/v1/leaves/{second.json()['id']}/decision", json={"approve": True}, headers=manager
    )
    assert over.status_code == 422, over.text
    # Rejecting is always allowed - it frees balance rather than spending it.
    rejected = await client.post(
        f"/api/v1/leaves/{second.json()['id']}/decision", json={"approve": False}, headers=manager
    )
    assert rejected.status_code == 200, rejected.text


async def test_overlapping_your_own_leave_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    start = _next_weekday(0)
    first = await _apply(client, settings, seed.report, _body("planned", start, start))
    assert first.status_code == 201
    clash = await _apply(client, settings, seed.report, _body("sick", start, start))
    assert clash.status_code == 422, clash.text
    assert "overlaps" in clash.json()["error"]["message"]
    # Someone else's leave on the same day is not a clash.
    other = await _apply(client, settings, seed.outsider, _body("planned", start, start))
    assert other.status_code == 201, other.text


async def test_two_different_halves_of_one_day_do_not_clash(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    day = _next_weekday(0)
    morning = await _apply(client, settings, seed.report, _body("half_day", day, half="first_half"))
    afternoon = await _apply(
        client, settings, seed.report, _body("half_day", day, half="second_half")
    )
    again = await _apply(client, settings, seed.report, _body("half_day", day, half="first_half"))
    assert morning.status_code == 201
    assert afternoon.status_code == 201, afternoon.text
    assert again.status_code == 422


async def test_withdrawn_leave_does_not_block_new_dates(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = _body("planned", _next_weekday(0))
    first = await _apply(client, settings, seed.report, body)
    await client.post(
        f"/api/v1/leaves/{first.json()['id']}/withdraw",
        headers=auth_headers(settings, seed.report),
    )
    again = await _apply(client, settings, seed.report, body)
    assert again.status_code == 201, again.text


# --------------------------------------------------------------------------- #
# Cancelling approved leave.
# --------------------------------------------------------------------------- #
async def _approved_via_api(client: AsyncClient, settings: Settings, seed: _Seed) -> str:
    resp = await _apply(client, settings, seed.report, _body("planned", _next_weekday(0)))
    leave_id = resp.json()["id"]
    decided = await client.post(
        f"/api/v1/leaves/{leave_id}/decision",
        json={"approve": True},
        headers=auth_headers(settings, seed.manager),
    )
    assert decided.status_code == 200, decided.text
    return str(leave_id)


async def _notifications(db: AsyncSession, recipient: Employee) -> list[Notification]:
    rows = await db.execute(
        select(Notification).where(
            Notification.recipient_id == recipient.id,
            Notification.kind == NotificationKind.LEAVE_DECISION,
        )
    )
    return list(rows.scalars().all())


async def test_employee_cancels_their_future_approved_leave(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    leave_id = await _approved_via_api(client, settings, seed)
    mine = auth_headers(settings, seed.report)
    listed = await client.get("/api/v1/leaves", headers=mine)
    assert _row(listed.json(), leave_id)["can_cancel"] is True

    resp = await client.post(f"/api/v1/leaves/{leave_id}/cancel", headers=mine)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    balance = (await client.get("/api/v1/leaves/balance", headers=mine)).json()
    planned = next(b for b in balance["balances"] if b["leave_type"] == "planned")
    assert planned["used"] == 0
    # The approver hears about it; the employee is not told about their own act.
    assert any("cancelled" in n.title for n in await _notifications(db, seed.manager))


async def test_employee_cannot_cancel_leave_that_has_started(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    today = datetime.now(UTC).date()
    leave = await _approved_leave(db, seed.report, today - timedelta(days=1), today)

    resp = await client.post(
        f"/api/v1/leaves/{leave.id}/cancel", headers=auth_headers(settings, seed.report)
    )

    assert resp.status_code == 409, resp.text


async def test_hr_and_the_manager_cancel_started_leave_and_the_employee_is_told(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    today = datetime.now(UTC).date()
    by_hr = await _approved_leave(db, seed.report, today - timedelta(days=2), today)
    by_manager = await _approved_leave(db, seed.report, today - timedelta(days=5), today)

    hr_resp = await client.post(
        f"/api/v1/leaves/{by_hr.id}/cancel",
        json={"note": "Came back early"},
        headers=auth_headers(settings, hr),
    )
    mgr_resp = await client.post(
        f"/api/v1/leaves/{by_manager.id}/cancel", headers=auth_headers(settings, seed.manager)
    )

    assert hr_resp.status_code == 200, hr_resp.text
    assert mgr_resp.status_code == 200, mgr_resp.text
    told = await _notifications(db, seed.report)
    assert sum(1 for n in told if n.title == "Your leave was cancelled") == 2


async def test_out_of_scope_callers_cannot_cancel(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave_id = await _approved_via_api(client, settings, seed)
    for actor in (seed.outsider, seed.admin):
        resp = await client.post(
            f"/api/v1/leaves/{leave_id}/cancel", headers=auth_headers(settings, actor)
        )
        assert resp.status_code == 404, resp.text


async def test_only_approved_leave_can_be_cancelled(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await _apply(client, settings, seed.report, _body("planned", _next_weekday(0)))
    cancel = await client.post(
        f"/api/v1/leaves/{resp.json()['id']}/cancel",
        headers=auth_headers(settings, seed.manager),
    )
    assert cancel.status_code == 409


async def test_cancelled_leave_stops_counting_for_payroll(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    leave_id = await _approved_via_api(client, settings, seed)
    report_id = seed.report.id
    start = datetime.now(UTC) - timedelta(days=1)
    end = datetime.now(UTC) + timedelta(days=60)
    repo = LeaveRepository(db)
    assert (await repo.approved_paid_in_range([report_id], start, end)).get(report_id)

    await client.post(
        f"/api/v1/leaves/{leave_id}/cancel", headers=auth_headers(settings, seed.report)
    )

    db.expire_all()
    assert not (await repo.approved_paid_in_range([report_id], start, end)).get(report_id)


# --------------------------------------------------------------------------- #
# An inactive manager on file falls back to HR, like having no manager.
# --------------------------------------------------------------------------- #
async def test_inactive_manager_falls_back_to_hr(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    seed.manager.is_active = False
    await db.commit()

    resp = await _apply(client, settings, seed.report, _body("planned", _next_weekday(0)))

    assert resp.status_code == 201, resp.text
    rows = await db.execute(
        select(Notification.recipient_id).where(Notification.kind == NotificationKind.LEAVE_REQUEST)
    )
    recipients = set(rows.scalars().all())
    assert hr.id in recipients
    assert seed.manager.id not in recipients


def test_dates_without_a_zone_are_read_as_utc_days() -> None:
    """A caller that sends "2026-11-06" must land on the same UTC-midnight day
    the app stores, or the overlap and balance checks (which compare UTC days)
    miss it whenever the database runs in a non-UTC timezone."""
    from app.schemas.leave import LeaveCreate

    body = LeaveCreate.model_validate(
        {"leave_type": "unpaid", "start_date": "2026-11-06", "end_date": "2026-11-07T00:00:00"}
    )
    assert body.start_date == datetime(2026, 11, 6, tzinfo=UTC)
    assert body.end_date == datetime(2026, 11, 7, tzinfo=UTC)
    zoned = LeaveCreate.model_validate(
        {
            "leave_type": "unpaid",
            "start_date": "2026-11-06T00:00:00Z",
            "end_date": "2026-11-06T00:00:00Z",
        }
    )
    assert zoned.start_date.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------- #
# Leave in the NEXT leave year spends that year's quota, never nothing.
# --------------------------------------------------------------------------- #
def _next_year_monday(*, weeks: int = 0) -> date:
    """A Monday in the next leave year (which opens on 1 April)."""
    opens = _leave_year_window(datetime.now(UTC).date())[1] + timedelta(days=1)
    return opens + timedelta(days=(0 - opens.weekday()) % 7 + 7 * weeks)


async def test_leave_in_the_next_leave_year_is_capped_by_that_years_quota(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    monday = _next_year_monday()

    # Four weeks is 20 working days against the default planned quota of 8.
    resp = await _apply(
        client, settings, seed.report, _body("planned", monday, monday + timedelta(days=25))
    )

    assert resp.status_code == 422, resp.text
    message = resp.json()["error"]["message"]
    assert message.startswith("Not enough Planned leave for the ")
    assert "leave year: 20 days requested" in message


async def test_next_years_bookings_spend_next_years_balance_not_this_years(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    monday = _next_year_monday()
    week = (monday, monday + timedelta(days=4))  # 5 working days

    first = await _apply(client, settings, seed.report, _body("planned", *week))
    # Another 5 in the same next year overspends it (5 + 5 > 8) ...
    later = _next_year_monday(weeks=2)
    second = await _apply(
        client, settings, seed.report, _body("planned", later, later + timedelta(days=4))
    )
    # ... while this year's balance is untouched by either.
    this_year = _next_weekday(0)
    now = await _apply(
        client,
        settings,
        seed.report,
        _body("planned", this_year, this_year + timedelta(days=4)),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 422, second.text
    assert now.status_code == 201, now.text


async def test_a_request_spanning_31_march_checks_both_leave_years(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Only two days fall in this leave year; the rest must fit next year's."""
    year_end = _leave_year_window(datetime.now(UTC).date())[1]
    start = year_end - timedelta(days=1)

    resp = await _apply(
        client, settings, seed.report, _body("planned", start, year_end + timedelta(days=30))
    )

    assert resp.status_code == 422, resp.text
    assert "for the " in resp.json()["error"]["message"]


# --------------------------------------------------------------------------- #
# Approving: only APPROVED leave blocks, worded for the approver.
# --------------------------------------------------------------------------- #
async def _pending_leave(db: AsyncSession, employee: Employee, day: date) -> Leave:
    """A pending one-day leave written straight to the table (two overlapping
    ones can predate the overlap rule)."""
    leave = Leave(
        employee_id=employee.id,
        leave_type=LeaveType.PLANNED,
        start_date=datetime(day.year, day.month, day.day, tzinfo=UTC),
        end_date=datetime(day.year, day.month, day.day, tzinfo=UTC),
        status=LeaveStatus.SUBMITTED,
        reviewer_id=employee.manager_id,
    )
    db.add(leave)
    await db.commit()
    return leave


async def test_another_pending_request_does_not_block_approval(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    day = _next_weekday(0)
    first = await _pending_leave(db, seed.report, day)
    second = await _pending_leave(db, seed.report, day)
    mgr = auth_headers(settings, seed.manager)

    approved = await client.post(
        f"/api/v1/leaves/{first.id}/decision", json={"approve": True}, headers=mgr
    )
    clash = await client.post(
        f"/api/v1/leaves/{second.id}/decision", json={"approve": True}, headers=mgr
    )

    assert approved.status_code == 200, approved.text
    assert clash.status_code == 422, clash.text
    message = clash.json()["error"]["message"]
    assert message.startswith("Remy Report already has approved leave on ")
    assert "You " not in message


async def test_a_second_decision_is_a_conflict_and_notifies_nobody_again(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    resp = await _apply(client, settings, seed.report, _body("planned", _next_weekday(0)))
    leave_id = resp.json()["id"]
    mgr = auth_headers(settings, seed.manager)

    first = await client.post(
        f"/api/v1/leaves/{leave_id}/decision", json={"approve": True}, headers=mgr
    )
    again = await client.post(
        f"/api/v1/leaves/{leave_id}/decision", json={"approve": False}, headers=mgr
    )

    assert first.status_code == 200, first.text
    assert again.status_code == 409, again.text
    assert len(await _notifications(db, seed.report)) == 1


# --------------------------------------------------------------------------- #
# Cancelling: released payroll is frozen; the CURRENT manager is told.
# --------------------------------------------------------------------------- #
async def test_cancelling_leave_in_a_released_payroll_month_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    today = datetime.now(UTC).date()
    leave = await _approved_leave(db, seed.report, today - timedelta(days=1), today)
    month = f"{today:%Y-%m}"
    db.add(Payslip(employee_id=seed.outsider.id, period_month=month))
    await db.commit()

    resp = await client.post(
        f"/api/v1/leaves/{leave.id}/cancel", headers=auth_headers(settings, seed.manager)
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["message"] == (
        f"Payroll for {month} is already released; correct it through a payroll adjustment."
    )
    await db.refresh(leave)
    assert leave.status is LeaveStatus.APPROVED


async def test_cancelling_tells_the_current_manager_not_the_old_approver(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    new_manager = Employee(
        hr_external_id="hr-new-manager",
        work_email="new.manager@corp.test",
        full_name="Nia New",
        role=Role.MANAGER,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(new_manager)
    await db.commit()
    leave_id = await _approved_via_api(client, settings, seed)  # approved by seed.manager
    seed.report.manager_id = new_manager.id
    await db.commit()

    resp = await client.post(
        f"/api/v1/leaves/{leave_id}/cancel", headers=auth_headers(settings, hr)
    )

    assert resp.status_code == 200, resp.text
    assert any("cancelled" in n.title for n in await _notifications(db, new_manager))
    assert not any("cancelled" in n.title for n in await _notifications(db, seed.manager))
    assert any(n.title == "Your leave was cancelled" for n in await _notifications(db, seed.report))


async def test_cancelling_falls_back_to_hr_without_an_active_manager(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    leave_id = await _approved_via_api(client, settings, seed)
    seed.manager.is_active = False
    await db.commit()

    resp = await client.post(
        f"/api/v1/leaves/{leave_id}/cancel", headers=auth_headers(settings, seed.report)
    )

    assert resp.status_code == 200, resp.text
    assert any("cancelled" in n.title for n in await _notifications(db, hr))
    assert not any("cancelled" in n.title for n in await _notifications(db, seed.manager))

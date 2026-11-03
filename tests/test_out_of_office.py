"""Out of office — working away from the office, approved by the manager.

Authorization is the point (CLAUDE.md §9): the requester's own manager or HR may
decide, nobody else, and never your own. The other half is arithmetic: an
approved day has to reach attendance and payroll, because a day at the warehouse
leaves no punch behind and would otherwise be paid as absence.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

URL = "/api/v1/attendance/out-of-office"


def _body(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "start_day": "2026-06-15",
        "end_day": "2026-06-15",
        "portion": "full_day",
        "location": "Printing warehouse",
        "reason": "Print testing for the new run",
    }
    base.update(over)
    return base


async def _add_hr(db: AsyncSession) -> Employee:
    hr = Employee(
        hr_external_id="hr-hr",
        work_email="hr@corp.test",
        full_name="Holly HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    return hr


async def _request_as(
    client: AsyncClient, settings: Settings, who: Employee, **over: object
) -> dict[str, object]:
    resp = await client.post(URL, json=_body(**over), headers=auth_headers(settings, who))
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


# ---- authorization --------------------------------------------------------- #


async def test_requires_authentication(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.post(URL, json=_body())).status_code == 401
    assert (await client.get(URL)).status_code == 401


async def test_you_cannot_approve_your_own(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Approving your own would be writing yourself a paid day."""
    entry = await _request_as(client, settings, seed.report)
    resp = await client.post(
        f"{URL}/{entry['id']}/review",
        json={"approve": True},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 403


async def test_an_unrelated_person_cannot_approve(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Out of scope entirely — must not even reveal that the entry exists."""
    entry = await _request_as(client, settings, seed.report)
    resp = await client.post(
        f"{URL}/{entry['id']}/review",
        json={"approve": True},
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 404


async def test_the_reporting_manager_can_approve(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    entry = await _request_as(client, settings, seed.report)
    resp = await client.post(
        f"{URL}/{entry['id']}/review",
        json={"approve": True, "review_note": "Sent him myself"},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert resp.json()["reviewed_by_id"] == str(seed.manager.id)


async def test_hr_can_approve_anyone(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _add_hr(db)
    entry = await _request_as(client, settings, seed.report)
    resp = await client.post(
        f"{URL}/{entry['id']}/review", json={"approve": True}, headers=auth_headers(settings, hr)
    )
    assert resp.status_code == 200


async def test_can_decide_is_the_servers_own_answer(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The queue must not offer a button the API would refuse."""
    await _request_as(client, settings, seed.report)
    mine = await client.get(URL, headers=auth_headers(settings, seed.report))
    assert [r["can_decide"] for r in mine.json()] == [False]
    theirs = await client.get(URL, headers=auth_headers(settings, seed.manager))
    assert [r["can_decide"] for r in theirs.json()] == [True]


# ---- rules ----------------------------------------------------------------- #


async def test_overlapping_request_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Two live entries on one day would each claim it, paying it twice."""
    await _request_as(client, settings, seed.report, start_day="2026-06-15", end_day="2026-06-17")
    resp = await client.post(
        URL,
        json=_body(start_day="2026-06-17", end_day="2026-06-18"),
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422
    assert "2026-06-15" in resp.json()["error"]["message"]


async def test_a_rejected_request_frees_the_dates_again(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    entry = await _request_as(client, settings, seed.report)
    await client.post(
        f"{URL}/{entry['id']}/review",
        json={"approve": False},
        headers=auth_headers(settings, seed.manager),
    )
    again = await client.post(URL, json=_body(), headers=auth_headers(settings, seed.report))
    assert again.status_code == 201


async def test_a_half_day_may_not_span_dates(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Half of a range is meaningless and would silently halve every day in it."""
    resp = await client.post(
        URL,
        json=_body(portion="half_day", start_day="2026-06-15", end_day="2026-06-17"),
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422


async def test_end_before_start_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        URL,
        json=_body(start_day="2026-06-17", end_day="2026-06-15"),
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422


async def test_reviewing_twice_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    entry = await _request_as(client, settings, seed.report)
    headers = auth_headers(settings, seed.manager)
    assert (
        await client.post(f"{URL}/{entry['id']}/review", json={"approve": True}, headers=headers)
    ).status_code == 200
    second = await client.post(
        f"{URL}/{entry['id']}/review", json={"approve": False}, headers=headers
    )
    assert second.status_code == 422


async def test_you_can_withdraw_your_own_pending_request(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    entry = await _request_as(client, settings, seed.report)
    mine = auth_headers(settings, seed.report)
    resp = await client.post(f"{URL}/{entry['id']}/cancel", headers=mine)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


async def test_you_cannot_withdraw_someone_elses(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    entry = await _request_as(client, settings, seed.report)
    resp = await client.post(
        f"{URL}/{entry['id']}/cancel", headers=auth_headers(settings, seed.manager)
    )
    assert resp.status_code == 404


# ---- it has to reach attendance and payroll -------------------------------- #


async def _approve(client: AsyncClient, settings: Settings, seed: _Seed, **over: object) -> None:
    entry = await _request_as(client, settings, seed.report, **over)
    resp = await client.post(
        f"{URL}/{entry['id']}/review",
        json={"approve": True},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 200


async def _day_row(
    client: AsyncClient, settings: Settings, seed: _Seed, day: str
) -> dict[str, object]:
    resp = await client.get(
        f"/api/v1/attendance/employee/{seed.report.id}",
        params={"start": day, "end": day},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["day"] == day]
    assert len(rows) == 1, rows
    return rows[0]  # type: ignore[no-any-return]


async def test_an_approved_day_appears_with_no_punch_behind_it(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Nobody punches at the warehouse, so the day has no session at all. It
    still has to produce a row, or the day silently would not exist."""
    await _approve(client, settings, seed)
    row = await _day_row(client, settings, seed, "2026-06-15")
    assert row["status"] == "out_of_office"
    assert row["out_of_office_credit"] == 1.0
    assert row["login_at"] is None


async def test_a_pending_request_changes_nothing(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Only approval credits the day - otherwise anyone could pay themselves."""
    await _request_as(client, settings, seed.report)
    row = await _day_row(client, settings, seed, "2026-06-15")
    assert row["status"] != "out_of_office"
    assert row["out_of_office_credit"] == 0.0


async def test_a_rejected_request_changes_nothing(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    entry = await _request_as(client, settings, seed.report)
    await client.post(
        f"{URL}/{entry['id']}/review",
        json={"approve": False},
        headers=auth_headers(settings, seed.manager),
    )
    row = await _day_row(client, settings, seed, "2026-06-15")
    assert row["status"] != "out_of_office"


async def test_a_half_day_away_with_no_office_time_credits_half(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _approve(client, settings, seed, portion="half_day")
    row = await _day_row(client, settings, seed, "2026-06-15")
    assert row["status"] == "out_of_office"
    assert row["out_of_office_credit"] == 0.5


async def test_a_multi_day_trip_credits_every_day(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _approve(client, settings, seed, start_day="2026-06-15", end_day="2026-06-17")
    for day in ("2026-06-15", "2026-06-16", "2026-06-17"):
        row = await _day_row(client, settings, seed, day)
        assert row["status"] == "out_of_office", day
        assert row["out_of_office_credit"] == 1.0, day


async def test_the_month_counts_it_as_worked_not_absent(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The reason the feature exists: these days must not be paid as absence."""
    before = await client.get(
        "/api/v1/attendance/report",
        params={"month": "2026-06"},
        headers=auth_headers(settings, seed.manager),
    )
    mine_before = next(r for r in before.json() if r["employee_id"] == str(seed.report.id))

    await _approve(client, settings, seed, start_day="2026-06-15", end_day="2026-06-17")

    after = await client.get(
        "/api/v1/attendance/report",
        params={"month": "2026-06"},
        headers=auth_headers(settings, seed.manager),
    )
    mine_after = next(r for r in after.json() if r["employee_id"] == str(seed.report.id))

    assert mine_after["out_of_office_days"] == 3.0
    # Those three days moved out of absence, not into it.
    assert mine_after["absent_days"] == mine_before["absent_days"] - 3


async def test_days_already_covered_by_approved_leave_are_refused(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Payroll counts worked days from attendance and paid-leave days from the
    leave table, independently - a day that is both would be credited twice.

    `min(elapsed_working, present + paid)` stops an overpayment, but the spare
    day silently forgives a real absence elsewhere in the month. Refuse the
    overlap instead of reconciling it later.
    """
    from datetime import UTC, datetime

    from app.models.leave import Leave, LeaveStatus, LeaveType

    db.add(
        Leave(
            employee_id=seed.report.id,
            leave_type=LeaveType.SICK,  # a PAID type - payroll counts it
            start_date=datetime(2026, 6, 15, tzinfo=UTC),
            end_date=datetime(2026, 6, 15, tzinfo=UTC),
            status=LeaveStatus.APPROVED,
        )
    )
    await db.commit()

    resp = await client.post(URL, json=_body(), headers=auth_headers(settings, seed.report))
    assert resp.status_code == 422
    assert "leave" in resp.json()["error"]["message"].lower()


async def test_unpaid_leave_does_not_block_out_of_office(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Unpaid leave is not counted as a paid day, so there is nothing to double."""
    from datetime import UTC, datetime

    from app.models.leave import Leave, LeaveStatus, LeaveType

    db.add(
        Leave(
            employee_id=seed.report.id,
            leave_type=LeaveType.UNPAID,
            start_date=datetime(2026, 6, 15, tzinfo=UTC),
            end_date=datetime(2026, 6, 15, tzinfo=UTC),
            status=LeaveStatus.APPROVED,
        )
    )
    await db.commit()

    resp = await client.post(URL, json=_body(), headers=auth_headers(settings, seed.report))
    assert resp.status_code == 201

"""Withdrawing an already-approved leave.

Giving back granted time off is a request the reporting manager decides, not
something the employee does alone (the team planned around the absence, and
after the leave has started it also rewrites attendance and pay).

Pinned here:
- only the owner may ask, and only on an APPROVED leave;
- the days stay spent while the request is pending - asking must not refund;
- only the people who could have decided the leave may decide the withdrawal,
  which is the manager or HR, never the requester and never an admin who is
  neither (`may_decide`);
- granting cancels the leave, so the balance and payroll see the same CANCELLED
  row a direct cancellation produces;
- a month whose payslips are released is refused on both halves of the cycle.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee
from app.models.payslip import Payslip
from tests.conftest import _Seed, auth_headers
from tests.test_leave_rules import (
    _apply,
    _approved_leave,
    _approved_via_api,
    _body,
    _hr,
    _next_weekday,
    _row,
)

_REASON = {"reason": "Trip was called off"}


async def _ask(client: AsyncClient, settings: Settings, who: Employee, leave_id: str):
    return await client.post(
        f"/api/v1/leaves/{leave_id}/withdrawal",
        json=_REASON,
        headers=auth_headers(settings, who),
    )


async def _decide(
    client: AsyncClient, settings: Settings, who: Employee, leave_id: str, *, approve: bool
):
    return await client.post(
        f"/api/v1/leaves/{leave_id}/withdrawal/decision",
        json={"approve": approve, "note": "noted"},
        headers=auth_headers(settings, who),
    )


async def _planned_used(client: AsyncClient, settings: Settings, who: Employee) -> float:
    balance = (
        await client.get("/api/v1/leaves/balance", headers=auth_headers(settings, who))
    ).json()
    planned = next(b for b in balance["balances"] if b["leave_type"] == "planned")
    used = planned["used"]
    assert isinstance(used, int | float)
    return float(used)


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #
async def test_the_owner_can_ask_and_the_days_stay_spent_until_decided(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave_id = await _approved_via_api(client, settings, seed)
    before = await _planned_used(client, settings, seed.report)
    assert before > 0

    resp = await _ask(client, settings, seed.report, leave_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["withdrawal_status"] == "requested"
    assert body["status"] == "approved"  # still granted until someone decides
    assert body["withdrawal_reason"] == _REASON["reason"]
    assert await _planned_used(client, settings, seed.report) == before


async def test_a_reason_is_required(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    leave_id = await _approved_via_api(client, settings, seed)
    resp = await client.post(
        f"/api/v1/leaves/{leave_id}/withdrawal",
        json={"reason": ""},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422


async def test_nobody_else_can_ask_on_your_behalf(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """404, not 403: whether someone else's leave exists is not the caller's
    business (rule 7). The manager wanting it gone has `cancel` for that."""
    hr = await _hr(db)
    leave_id = await _approved_via_api(client, settings, seed)
    for actor in (seed.manager, hr, seed.admin, seed.outsider):
        resp = await _ask(client, settings, actor, leave_id)
        assert resp.status_code == 404, f"{actor.full_name}: {resp.text}"


async def test_only_an_approved_leave_can_be_withdrawn(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    submitted = await _apply(client, settings, seed.report, _body("planned", _next_weekday(0)))
    resp = await _ask(client, settings, seed.report, submitted.json()["id"])
    assert resp.status_code == 409


async def test_a_second_request_cannot_be_stacked(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave_id = await _approved_via_api(client, settings, seed)
    assert (await _ask(client, settings, seed.report, leave_id)).status_code == 200
    assert (await _ask(client, settings, seed.report, leave_id)).status_code == 409


async def test_a_refused_request_can_be_made_again(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Circumstances change; one "no" should not bar asking for good."""
    leave_id = await _approved_via_api(client, settings, seed)
    await _ask(client, settings, seed.report, leave_id)
    await _decide(client, settings, seed.manager, leave_id, approve=False)

    again = await _ask(client, settings, seed.report, leave_id)

    assert again.status_code == 200, again.text
    assert again.json()["withdrawal_status"] == "requested"


# --------------------------------------------------------------------------- #
# Deciding
# --------------------------------------------------------------------------- #
async def test_the_manager_grants_it_and_the_days_come_back(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave_id = await _approved_via_api(client, settings, seed)
    await _ask(client, settings, seed.report, leave_id)

    resp = await _decide(client, settings, seed.manager, leave_id, approve=True)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["withdrawal_status"] == "approved"
    assert await _planned_used(client, settings, seed.report) == 0


async def test_refusing_leaves_the_leave_standing(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave_id = await _approved_via_api(client, settings, seed)
    before = await _planned_used(client, settings, seed.report)
    await _ask(client, settings, seed.report, leave_id)

    resp = await _decide(client, settings, seed.manager, leave_id, approve=False)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert resp.json()["withdrawal_status"] == "rejected"
    assert await _planned_used(client, settings, seed.report) == before


async def test_hr_can_decide_it_too(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    leave_id = await _approved_via_api(client, settings, seed)
    await _ask(client, settings, seed.report, leave_id)

    resp = await _decide(client, settings, hr, leave_id, approve=True)

    assert resp.status_code == 200, resp.text
    assert resp.json()["withdrawal_status"] == "approved"


async def test_the_requester_cannot_decide_their_own_withdrawal(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Segregation of duties: approving your own request would make the whole
    cycle decorative."""
    leave_id = await _approved_via_api(client, settings, seed)
    await _ask(client, settings, seed.report, leave_id)

    resp = await _decide(client, settings, seed.report, leave_id, approve=True)

    assert resp.status_code == 403, resp.text


async def test_out_of_scope_callers_cannot_decide(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Admin included: leave is personal data, and admin is not org-wide for it
    (`_personal_scope_clause`), so the leave is not even visible to them."""
    leave_id = await _approved_via_api(client, settings, seed)
    await _ask(client, settings, seed.report, leave_id)
    for actor in (seed.admin, seed.outsider):
        resp = await _decide(client, settings, actor, leave_id, approve=True)
        assert resp.status_code == 404, f"{actor.full_name}: {resp.text}"


async def test_deciding_without_a_request_is_a_conflict(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave_id = await _approved_via_api(client, settings, seed)
    resp = await _decide(client, settings, seed.manager, leave_id, approve=True)
    assert resp.status_code == 409


async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    for path in ("withdrawal", "withdrawal/decision"):
        resp = await client.post(f"/api/v1/leaves/{seed.report.id}/{path}", json=_REASON)
        assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Payroll already released
# --------------------------------------------------------------------------- #
async def _release_payroll(db: AsyncSession, employee: Employee, day: date) -> None:
    db.add(
        Payslip(
            employee_id=employee.id,
            period_month=f"{day:%Y-%m}",
            released_at=datetime.now(UTC),
        )
    )
    await db.commit()


async def test_a_released_month_refuses_the_request(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Refused when asked, not when decided: there is no point sending a manager
    a decision they are not allowed to make."""
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    leave = await _approved_leave(db, seed.report, yesterday, yesterday)
    await _release_payroll(db, seed.report, yesterday)

    resp = await _ask(client, settings, seed.report, str(leave.id))

    assert resp.status_code == 409
    assert "already released" in resp.json()["error"]["message"]


# --------------------------------------------------------------------------- #
# The flags the UI renders from
# --------------------------------------------------------------------------- #
async def test_the_permission_flags_follow_the_same_rules(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    leave_id = await _approved_via_api(client, settings, seed)
    mine = auth_headers(settings, seed.report)
    theirs = auth_headers(settings, seed.manager)

    row = _row((await client.get("/api/v1/leaves", headers=mine)).json(), leave_id)
    assert row["can_request_withdrawal"] is True
    assert row["can_decide_withdrawal"] is False

    await _ask(client, settings, seed.report, leave_id)

    mine_now = _row((await client.get("/api/v1/leaves", headers=mine)).json(), leave_id)
    theirs_now = _row((await client.get("/api/v1/leaves", headers=theirs)).json(), leave_id)
    assert mine_now["can_request_withdrawal"] is False  # one is already pending
    assert mine_now["can_decide_withdrawal"] is False  # never your own
    assert theirs_now["can_decide_withdrawal"] is True

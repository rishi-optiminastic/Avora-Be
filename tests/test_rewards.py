"""Rewards - points, the catalogue, and spending them.

Recognition used to be a badge and a note. It now carries POINTS that buy real
things, which changes what the code has to be careful about: a balance is money
in all but name, and the tests here are mostly about it never being wrong.

The rules pinned:

* a balance is `earned - spent`, derived from the rows, never stored;
* REQUESTED already holds the points, so the same balance cannot be spent twice
  while HR works through the queue;
* DECLINED and CANCELLED give them back, so refusing a request never fines the
  person who made it;
* the balance is re-checked at APPROVAL, because minutes pass;
* taking back a recognition whose points are already committed is refused;
* points and prices are snapshotted, so repricing never rewrites history.

Authorization (CLAUDE.md §9): everyone may read the wall and the catalogue -
the recognition feed behind it is org-wide - but only HR and admins publish
rewards or decide redemptions.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.recognition_badges import PEER_AWARDS_PER_MONTH
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

URL = "/api/v1/rewards"
RECOGNITION = "/api/v1/recognition"

#: Win of the Week, awarded by the recipient's own manager.
_FULL_AWARD = 20


async def _add_hr(db: AsyncSession) -> Employee:
    hr = Employee(
        hr_external_id="hr-rewards",
        work_email="hr-rewards@corp.test",
        full_name="Holly HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    return hr


async def _award(
    client: AsyncClient,
    settings: Settings,
    giver: Employee,
    to: Employee,
    badge: str = "win_of_the_week",
) -> dict[str, object]:
    resp = await client.post(
        RECOGNITION,
        json={"employee_id": str(to.id), "badge": badge, "note": "Earned it."},
        headers=auth_headers(settings, giver),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def _publish(
    client: AsyncClient, settings: Settings, who: Employee, *, cost: int, stock: int | None = None
) -> str:
    body: dict[str, object] = {"title": "Half-day off", "points_cost": cost}
    if stock is not None:
        body["stock"] = stock
    resp = await client.post(f"{URL}/catalogue", json=body, headers=auth_headers(settings, who))
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _balance(client: AsyncClient, settings: Settings, who: Employee) -> dict[str, object]:
    resp = await client.get(f"{URL}/me/balance", headers=auth_headers(settings, who))
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# Who may do what
# --------------------------------------------------------------------------- #
async def test_every_reward_endpoint_requires_authentication(
    client: AsyncClient, seed: _Seed
) -> None:
    for method, path in (
        ("get", f"{URL}/me/balance"),
        ("get", f"{URL}/standings"),
        ("get", f"{URL}/catalogue"),
        ("post", f"{URL}/catalogue"),
        ("get", f"{URL}/redemptions/me"),
        ("get", f"{URL}/redemptions/queue"),
        ("post", f"{URL}/redemptions"),
    ):
        resp = await getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 401, f"{method} {path}: {resp.status_code}"


async def test_only_hr_and_admins_publish_rewards(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.post(
            f"{URL}/catalogue",
            json={"title": "A thing", "points_cost": 10},
            headers=auth_headers(settings, actor),
        )
        assert resp.status_code == 403, f"{actor.full_name}: {resp.text}"


async def test_only_hr_and_admins_see_the_queue(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.get(f"{URL}/redemptions/queue", headers=auth_headers(settings, actor))
        assert resp.status_code == 403, f"{actor.full_name}: {resp.text}"


async def test_only_hr_and_admins_decide_a_redemption(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    item = await _publish(client, settings, seed.admin, cost=10)
    await _award(client, settings, seed.manager, seed.report)
    asked = await client.post(
        f"{URL}/redemptions",
        json={"item_id": item},
        headers=auth_headers(settings, seed.report),
    )
    assert asked.status_code == 201, asked.text
    rid = asked.json()["id"]

    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.post(
            f"{URL}/redemptions/{rid}/decision",
            json={"status": "approved"},
            headers=auth_headers(settings, actor),
        )
        assert resp.status_code == 403, f"{actor.full_name}: {resp.text}"


async def test_anyone_may_read_the_wall_and_the_catalogue(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Deliberately org-wide, like the recognition feed it is built from. A
    leaderboard nobody can check is not a leaderboard."""
    await _publish(client, settings, seed.admin, cost=10)
    for path in (f"{URL}/standings", f"{URL}/catalogue"):
        resp = await client.get(path, headers=auth_headers(settings, seed.outsider))
        assert resp.status_code == 200, f"{path}: {resp.text}"


# --------------------------------------------------------------------------- #
# The balance
# --------------------------------------------------------------------------- #
async def test_points_accumulate_from_recognition(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _award(client, settings, seed.manager, seed.report)
    await _award(client, settings, seed.manager, seed.report, badge="consistency_star")

    body = await _balance(client, settings, seed.report)
    assert body["earned"] == _FULL_AWARD + 50
    assert body["spent"] == 0
    assert body["available"] == _FULL_AWARD + 50


async def test_a_fresh_employee_has_nothing(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = await _balance(client, settings, seed.outsider)
    assert body == {
        "employee_id": str(seed.outsider.id),
        "earned": 0,
        "spent": 0,
        "available": 0,
        "peer_awards_left": PEER_AWARDS_PER_MONTH,
    }


async def test_requesting_holds_the_points_immediately(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Otherwise the same 20 points buy four things while HR reads the queue."""
    await _award(client, settings, seed.manager, seed.report)
    item = await _publish(client, settings, seed.admin, cost=_FULL_AWARD)

    asked = await client.post(
        f"{URL}/redemptions", json={"item_id": item}, headers=auth_headers(settings, seed.report)
    )
    assert asked.status_code == 201, asked.text

    body = await _balance(client, settings, seed.report)
    assert body["spent"] == _FULL_AWARD
    assert body["available"] == 0


async def test_the_same_points_cannot_be_spent_twice(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _award(client, settings, seed.manager, seed.report)
    item = await _publish(client, settings, seed.admin, cost=_FULL_AWARD)
    first = await client.post(
        f"{URL}/redemptions", json={"item_id": item}, headers=auth_headers(settings, seed.report)
    )
    assert first.status_code == 201

    second = await client.post(
        f"{URL}/redemptions", json={"item_id": item}, headers=auth_headers(settings, seed.report)
    )

    assert second.status_code == 409, second.text
    assert "points" in second.json()["error"]["message"].lower()


async def test_you_cannot_redeem_what_you_cannot_afford(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    item = await _publish(client, settings, seed.admin, cost=500)
    resp = await client.post(
        f"{URL}/redemptions", json={"item_id": item}, headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 409, resp.text


async def test_declining_gives_the_points_back(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Refusing a request must never quietly fine the person who made it."""
    await _award(client, settings, seed.manager, seed.report)
    item = await _publish(client, settings, seed.admin, cost=_FULL_AWARD)
    rid = (
        await client.post(
            f"{URL}/redemptions",
            json={"item_id": item},
            headers=auth_headers(settings, seed.report),
        )
    ).json()["id"]

    decided = await client.post(
        f"{URL}/redemptions/{rid}/decision",
        json={"status": "declined", "note": "Not this quarter."},
        headers=auth_headers(settings, seed.admin),
    )
    assert decided.status_code == 200, decided.text

    body = await _balance(client, settings, seed.report)
    assert body["spent"] == 0
    assert body["available"] == _FULL_AWARD


async def test_cancelling_your_own_request_gives_the_points_back(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _award(client, settings, seed.manager, seed.report)
    item = await _publish(client, settings, seed.admin, cost=_FULL_AWARD)
    rid = (
        await client.post(
            f"{URL}/redemptions",
            json={"item_id": item},
            headers=auth_headers(settings, seed.report),
        )
    ).json()["id"]

    resp = await client.post(
        f"{URL}/redemptions/{rid}/cancel", headers=auth_headers(settings, seed.report)
    )

    assert resp.status_code == 200, resp.text
    assert (await _balance(client, settings, seed.report))["available"] == _FULL_AWARD


async def test_you_cannot_cancel_somebody_elses_request(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """404 rather than 403: whose redemption it is is not the caller's business."""
    await _award(client, settings, seed.manager, seed.report)
    item = await _publish(client, settings, seed.admin, cost=_FULL_AWARD)
    rid = (
        await client.post(
            f"{URL}/redemptions",
            json={"item_id": item},
            headers=auth_headers(settings, seed.report),
        )
    ).json()["id"]

    resp = await client.post(
        f"{URL}/redemptions/{rid}/cancel", headers=auth_headers(settings, seed.outsider)
    )
    assert resp.status_code == 404


async def test_a_decided_request_can_no_longer_be_cancelled(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _award(client, settings, seed.manager, seed.report, badge="consistency_star")
    item = await _publish(client, settings, seed.admin, cost=10)
    rid = (
        await client.post(
            f"{URL}/redemptions",
            json={"item_id": item},
            headers=auth_headers(settings, seed.report),
        )
    ).json()["id"]
    await client.post(
        f"{URL}/redemptions/{rid}/decision",
        json={"status": "approved"},
        headers=auth_headers(settings, seed.admin),
    )

    resp = await client.post(
        f"{URL}/redemptions/{rid}/cancel", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 409, resp.text


# --------------------------------------------------------------------------- #
# Snapshots - repricing never rewrites history
# --------------------------------------------------------------------------- #
async def test_repricing_a_reward_does_not_change_what_was_already_paid(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _award(client, settings, seed.manager, seed.report, badge="consistency_star")
    item = await _publish(client, settings, seed.admin, cost=20)
    rid = (
        await client.post(
            f"{URL}/redemptions",
            json={"item_id": item},
            headers=auth_headers(settings, seed.report),
        )
    ).json()["id"]

    bumped = await client.patch(
        f"{URL}/catalogue/{item}",
        json={"points_cost": 999, "title": "Renamed"},
        headers=auth_headers(settings, seed.admin),
    )
    assert bumped.status_code == 200, bumped.text

    mine = await client.get(f"{URL}/redemptions/me", headers=auth_headers(settings, seed.report))
    row = next(r for r in mine.json() if r["id"] == rid)
    assert row["points_spent"] == 20, "what it cost when it was asked for"
    assert row["item_title"] == "Half-day off", "and what it was called"


async def test_taking_back_a_recognition_whose_points_are_spent_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The points are already committed to a reward; removing them would leave
    the recipient owing Avora points they were told they had."""
    award = await _award(client, settings, seed.manager, seed.report)
    item = await _publish(client, settings, seed.admin, cost=_FULL_AWARD)
    asked = await client.post(
        f"{URL}/redemptions", json={"item_id": item}, headers=auth_headers(settings, seed.report)
    )
    assert asked.status_code == 201

    resp = await client.delete(
        f"{RECOGNITION}/{award['id']}", headers=auth_headers(settings, seed.manager)
    )

    assert resp.status_code == 422, resp.text
    assert "reward" in resp.json()["error"]["message"].lower()


async def test_taking_back_an_unspent_recognition_still_works(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    award = await _award(client, settings, seed.manager, seed.report)

    resp = await client.delete(
        f"{RECOGNITION}/{award['id']}", headers=auth_headers(settings, seed.manager)
    )

    assert resp.status_code in (200, 204), resp.text
    assert (await _balance(client, settings, seed.report))["earned"] == 0


# --------------------------------------------------------------------------- #
# Stock
# --------------------------------------------------------------------------- #
async def test_an_out_of_stock_reward_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _add_hr(db)
    item = await _publish(client, settings, seed.admin, cost=10, stock=1)
    await _award(client, settings, seed.manager, seed.report, badge="consistency_star")
    await _award(client, settings, seed.admin, seed.outsider, badge="consistency_star")

    first = await client.post(
        f"{URL}/redemptions", json={"item_id": item}, headers=auth_headers(settings, seed.report)
    )
    assert first.status_code == 201
    # Stock is held by APPROVAL, not by asking - otherwise one person could
    # empty the shelf by requesting everything and never being approved.
    approved = await client.post(
        f"{URL}/redemptions/{first.json()['id']}/decision",
        json={"status": "approved"},
        headers=auth_headers(settings, hr),
    )
    assert approved.status_code == 200, approved.text

    second = await client.post(
        f"{URL}/redemptions", json={"item_id": item}, headers=auth_headers(settings, seed.outsider)
    )
    assert second.status_code == 409, second.text
    assert "stock" in second.json()["error"]["message"].lower()


# --------------------------------------------------------------------------- #
# The peer allowance
# --------------------------------------------------------------------------- #
async def test_peer_awards_are_rationed_per_month(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Points buy real things, so the supply has to be bounded."""
    for _ in range(PEER_AWARDS_PER_MONTH):
        await _award(client, settings, seed.report, seed.outsider)

    resp = await client.post(
        RECOGNITION,
        json={
            "employee_id": str(seed.outsider.id),
            "badge": "team_player",
            "note": "One too many.",
        },
        headers=auth_headers(settings, seed.report),
    )

    assert resp.status_code == 422, resp.text
    assert "this month" in resp.json()["error"]["message"].lower()
    assert (await _balance(client, settings, seed.report))["peer_awards_left"] == 0


async def test_a_manager_recognising_their_own_reports_is_not_rationed(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """That is the job, and it is already visible to the whole org."""
    for _ in range(PEER_AWARDS_PER_MONTH + 3):
        await _award(client, settings, seed.manager, seed.report)

    assert (await _balance(client, settings, seed.manager))["peer_awards_left"] == (
        PEER_AWARDS_PER_MONTH
    )


# --------------------------------------------------------------------------- #
# The wall
# --------------------------------------------------------------------------- #
async def test_the_wall_ranks_by_points_not_by_count(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Two small badges must not outrank one big one.

    Counting awards would put the outsider first on 2 to 1. Ranking by points
    is what makes a Client Appreciation mean more than a couple of weekly
    nods, which is the whole reason the badges carry different values.
    """
    await _award(client, settings, seed.manager, seed.report, badge="client_appreciation")
    for _ in range(2):
        await _award(client, settings, seed.admin, seed.outsider, badge="win_of_the_week")

    rows = (
        await client.get(f"{URL}/standings", headers=auth_headers(settings, seed.report))
    ).json()

    assert rows[0]["employee_id"] == str(seed.report.id)
    assert rows[0]["points"] == 60, "one Client Appreciation"
    assert rows[0]["awards"] == 1, "on a single award"
    assert rows[1]["employee_id"] == str(seed.outsider.id)
    assert rows[1]["points"] == 40, "two Wins of the Week"
    assert rows[1]["awards"] == 2, "on twice as many"


# --------------------------------------------------------------------------- #
# The whiteboard
#
# Ranked by award COUNT, not points - unlike the individual wall. The question
# a whiteboard answers is "which department is recognising each other most",
# and a single high-value badge should not win a department the week.
# --------------------------------------------------------------------------- #
async def _in_department(db: AsyncSession, person: Employee, name: str) -> None:
    person.department = name
    await db.commit()


async def test_the_whiteboard_is_readable_by_everyone(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Org-wide like the feed. A board nobody can see is not a board."""
    resp = await client.get(f"{URL}/whiteboard", headers=auth_headers(settings, seed.outsider))
    assert resp.status_code == 200, resp.text
    assert resp.json()["period"] == "month"


async def test_the_whiteboard_requires_authentication(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get(f"{URL}/whiteboard")).status_code == 401


async def test_departments_rank_by_award_count_not_points(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _in_department(db, seed.report, "Engineering")
    await _in_department(db, seed.outsider, "Finance")
    # Finance earns more POINTS from one big badge; Engineering gives more.
    await _award(client, settings, seed.admin, seed.outsider, badge="client_appreciation")
    for _ in range(3):
        await _award(client, settings, seed.manager, seed.report)

    rows = (
        await client.get(f"{URL}/whiteboard", headers=auth_headers(settings, seed.report))
    ).json()["departments"]

    assert [r["department"] for r in rows] == ["Engineering", "Finance"]
    assert rows[0]["awards"] == 3
    assert rows[0]["points"] == 60, "three Wins of the Week"
    assert rows[1]["awards"] == 1
    assert rows[1]["points"] == 60, "one Client Appreciation - the same points, fewer awards"


async def test_the_leading_department_is_crowned_by_the_server(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Decided once, so every screen crowns the same one."""
    await _in_department(db, seed.report, "Engineering")
    await _in_department(db, seed.outsider, "Finance")
    await _award(client, settings, seed.manager, seed.report)
    await _award(client, settings, seed.manager, seed.report)
    await _award(client, settings, seed.admin, seed.outsider)

    rows = (
        await client.get(f"{URL}/whiteboard", headers=auth_headers(settings, seed.report))
    ).json()["departments"]

    assert [r["leading"] for r in rows] == [True, False]


async def test_a_tie_crowns_nobody(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A shared first place is not a win, and picking one by name would be
    inventing a result."""
    await _in_department(db, seed.report, "Engineering")
    await _in_department(db, seed.outsider, "Finance")
    await _award(client, settings, seed.manager, seed.report)
    await _award(client, settings, seed.admin, seed.outsider)

    rows = (
        await client.get(f"{URL}/whiteboard", headers=auth_headers(settings, seed.report))
    ).json()["departments"]

    assert [r["leading"] for r in rows] == [False, False]


async def test_an_empty_board_crowns_nobody(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Nobody recognised yet must not produce a winner on zero awards."""
    rows = (
        await client.get(f"{URL}/whiteboard", headers=auth_headers(settings, seed.report))
    ).json()["departments"]
    assert all(not r["leading"] for r in rows)


async def test_the_board_counts_how_many_people_were_recognised(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Three badges to one person is not a department recognising each other,
    and the count is what says so."""
    await _in_department(db, seed.report, "Engineering")
    for _ in range(3):
        await _award(client, settings, seed.manager, seed.report)

    rows = (
        await client.get(f"{URL}/whiteboard", headers=auth_headers(settings, seed.report))
    ).json()["departments"]
    engineering = next(r for r in rows if r["department"] == "Engineering")

    assert engineering["awards"] == 3
    assert engineering["people"] == 1


async def test_somebody_with_no_department_is_left_off_rather_than_bucketed(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """`department` is free text and often empty. Inventing an "Unassigned"
    row would put a name on the board that does not exist in the org."""
    seed.report.department = None
    await db.commit()
    await _award(client, settings, seed.manager, seed.report)

    rows = (
        await client.get(f"{URL}/whiteboard", headers=auth_headers(settings, seed.report))
    ).json()["departments"]

    assert all(r["department"] for r in rows)


async def test_the_window_is_reported_back_with_the_board(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """So a board fetched for the week cannot be captioned as the month."""
    for period in ("week", "month", "all"):
        resp = await client.get(
            f"{URL}/whiteboard?period={period}", headers=auth_headers(settings, seed.report)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["period"] == period


async def test_an_unknown_window_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get(
        f"{URL}/whiteboard?period=fortnight", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 422

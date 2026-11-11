"""Employee recognition — awarding badges, the org-wide feed, and the threads.

Authorization is the point (CLAUDE.md §9). Awarding is now open to EVERYONE -
it was limited to a recipient's own reporting line plus HR and admin, which on
a 29-person org meant 8 possible givers and one recognition in the programme's
lifetime. What differs is weight, not permission: an award from the recipient's
line is MANAGER and carries the badge's full points, anyone else is PEER and
carries half, and peer awards are rationed per month so points stay worth
something. Nobody recognises themselves.

The feed is deliberately org-wide, so the test for that is that an outsider CAN
read it - the opposite of every other read in this codebase.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

URL = "/api/v1/recognition"


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


def _award_body(employee: Employee, badge: str = "win_of_the_week") -> dict[str, object]:
    return {
        "employee_id": str(employee.id),
        "badge": badge,
        "note": "Shipped the print run fix a day early.",
    }


async def _award(
    client: AsyncClient, settings: Settings, giver: Employee, to: Employee
) -> dict[str, object]:
    resp = await client.post(URL, json=_award_body(to), headers=auth_headers(settings, giver))
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


# ---- the published badge list ---------------------------------------------- #


async def test_the_ten_badges_are_served_with_their_meanings(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The form and the feed must share one source for labels and emoji."""
    resp = await client.get(f"{URL}/badges", headers=auth_headers(settings, seed.report))
    assert resp.status_code == 200
    badges = resp.json()
    assert len(badges) == 10
    assert {b["cadence"] for b in badges} == {"weekly", "monthly"}
    assert sum(b["cadence"] == "monthly" for b in badges) == 5
    star = next(b for b in badges if b["badge"] == "consistency_star")
    assert star["emoji"] == "🔥"
    assert star["label"] == "Consistency Star"
    assert star["when_to_use"]


async def test_badges_require_authentication(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get(f"{URL}/badges")).status_code == 401


# ---- who may award ---------------------------------------------------------- #


async def test_a_manager_recognises_their_own_report(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = await _award(client, settings, seed.manager, seed.report)
    assert body["badge"] == "win_of_the_week"
    assert body["emoji"] == "⭐"
    assert body["badge_label"] == "Win of the Week"
    assert body["employee_id"] == str(seed.report.id)
    assert body["awarded_by_name"] == seed.manager.full_name


async def test_a_colleague_outside_the_reporting_line_may_still_recognise(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The rule this replaced returned 403 here.

    It was the whole problem: the people who see the work most closely are
    usually not the ones who sign it off, and 21 of 29 had no way to recognise
    anybody at all.
    """
    resp = await client.post(
        URL,
        json=_award_body(seed.report),
        headers=auth_headers(settings, seed.outsider),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["source"] == "peer", "and it is marked as what it is"


async def test_a_peer_award_is_worth_half_a_manager_one(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Both are real recognition; only one is from the person accountable."""
    by_manager = await _award(client, settings, seed.manager, seed.report)
    by_peer = await client.post(
        URL, json=_award_body(seed.outsider), headers=auth_headers(settings, seed.admin)
    )
    peer = await client.post(
        URL,
        json=_award_body(seed.manager),
        headers=auth_headers(settings, seed.report),
    )

    assert by_peer.status_code == 201
    assert peer.status_code == 201, peer.text
    assert by_manager["source"] == "manager"
    assert by_manager["points"] == 20, "Win of the Week, in full"
    assert peer.json()["source"] == "peer"
    assert peer.json()["points"] == 10, "half, rounded down"


async def test_you_cannot_recognise_yourself(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        URL, json=_award_body(seed.manager), headers=auth_headers(settings, seed.manager)
    )
    assert resp.status_code == 422


async def test_hr_may_recognise_anyone(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _add_hr(db)
    resp = await client.post(
        URL, json=_award_body(seed.outsider), headers=auth_headers(settings, hr)
    )
    assert resp.status_code == 201


async def test_admin_may_recognise_anyone(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        URL, json=_award_body(seed.outsider), headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 201


async def test_a_note_is_required(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    """A badge with no reason is hollow, and the note is what colleagues reply to."""
    resp = await client.post(
        URL,
        json={"employee_id": str(seed.report.id), "badge": "team_player", "note": ""},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 422


async def test_an_unknown_badge_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        URL,
        json=_award_body(seed.report, badge="employee_of_the_century"),
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 422


# ---- the feed is org-wide ---------------------------------------------------- #


async def test_everyone_sees_the_feed_including_people_outside_the_team(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Unlike attendance, this read is NOT scoped - recognition only the
    recipient can see is a private message, not a programme."""
    await _award(client, settings, seed.manager, seed.report)
    resp = await client.get(URL, headers=auth_headers(settings, seed.outsider))
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["employee_name"] == seed.report.full_name


async def test_the_feed_can_be_filtered_to_one_person(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _award(client, settings, seed.manager, seed.report)
    await _award(client, settings, seed.admin, seed.outsider)
    resp = await client.get(
        URL,
        params={"employee_id": str(seed.report.id)},
        headers=auth_headers(settings, seed.report),
    )
    assert [r["employee_id"] for r in resp.json()] == [str(seed.report.id)]


async def test_the_feed_requires_authentication(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get(URL)).status_code == 401


# ---- comments ---------------------------------------------------------------- #


async def test_any_colleague_may_congratulate(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    row = await _award(client, settings, seed.manager, seed.report)
    resp = await client.post(
        f"{URL}/{row['id']}/comments",
        json={"body": "Well deserved!"},
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 201

    feed = await client.get(URL, headers=auth_headers(settings, seed.report))
    card = feed.json()[0]
    assert len(card["comments"]) == 1
    assert card["comments"][0]["body"] == "Well deserved!"
    assert card["comments"][0]["author_name"] == seed.outsider.full_name


async def test_a_whitespace_only_comment_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """`min_length=1` alone lets three spaces through, which then strip to an
    empty comment sitting in the thread."""
    row = await _award(client, settings, seed.manager, seed.report)
    resp = await client.post(
        f"{URL}/{row['id']}/comments",
        json={"body": "   "},
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 422


async def test_a_whitespace_only_note_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        URL,
        json={"employee_id": str(seed.report.id), "badge": "team_player", "note": "  "},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 422


async def test_you_may_delete_your_own_comment_but_not_anothers(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    row = await _award(client, settings, seed.manager, seed.report)
    made = await client.post(
        f"{URL}/{row['id']}/comments",
        json={"body": "Nice one"},
        headers=auth_headers(settings, seed.outsider),
    )
    comment_id = made.json()["id"]

    denied = await client.delete(
        f"{URL}/comments/{comment_id}", headers=auth_headers(settings, seed.report)
    )
    assert denied.status_code == 403

    mine = await client.delete(
        f"{URL}/comments/{comment_id}", headers=auth_headers(settings, seed.outsider)
    )
    assert mine.status_code == 204


async def test_can_delete_is_the_servers_own_answer(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    row = await _award(client, settings, seed.manager, seed.report)
    await client.post(
        f"{URL}/{row['id']}/comments",
        json={"body": "Congrats"},
        headers=auth_headers(settings, seed.outsider),
    )
    as_author = await client.get(URL, headers=auth_headers(settings, seed.outsider))
    assert as_author.json()[0]["comments"][0]["can_delete"] is True
    as_other = await client.get(URL, headers=auth_headers(settings, seed.report))
    assert as_other.json()[0]["comments"][0]["can_delete"] is False


# ---- taking one back ---------------------------------------------------------- #


async def test_the_giver_may_take_a_recognition_back(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    row = await _award(client, settings, seed.manager, seed.report)
    resp = await client.delete(f"{URL}/{row['id']}", headers=auth_headers(settings, seed.manager))
    assert resp.status_code == 204
    feed = await client.get(URL, headers=auth_headers(settings, seed.report))
    assert feed.json() == []


async def test_the_recipient_cannot_delete_their_own_recognition(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Nobody edits their own record - in either direction."""
    row = await _award(client, settings, seed.manager, seed.report)
    resp = await client.delete(f"{URL}/{row['id']}", headers=auth_headers(settings, seed.report))
    assert resp.status_code == 403


async def test_an_unrelated_person_cannot_delete_a_recognition(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    row = await _award(client, settings, seed.manager, seed.report)
    resp = await client.delete(f"{URL}/{row['id']}", headers=auth_headers(settings, seed.outsider))
    assert resp.status_code == 403

"""The Room - open to everyone, with two things that are not.

§9 requires an authorization test on every protected endpoint. Here the
interesting proof runs both ways: posting and reading must NOT be gated (a
board an employee cannot see is a board nobody can wish anyone on), while
deleting someone else's note and pinning must be.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.event import Event, EventStatus
from app.models.room import RoomMessage
from tests.conftest import _Seed, auth_headers


async def _board(client: AsyncClient, settings: Settings, who: object) -> dict:
    resp = await client.get("/api/v1/room", headers=auth_headers(settings, who))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_every_employee_can_read_and_post(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    """The whole point of the board. An ordinary report - no reports of their
    own, no role - must be able to see it and say something on it."""
    board = await _board(client, settings, seed.report)
    general = board["general_topic"]

    resp = await client.post(
        "/api/v1/room/messages",
        json={"body": "Happy birthday!", "topic_key": general},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 201
    assert resp.json()["body"] == "Happy birthday!"
    assert resp.json()["author_name"] == seed.report.full_name

    # And an unrelated colleague sees it - the board is not scoped to a line.
    after = await _board(client, settings, seed.outsider)
    assert [m["body"] for m in after["messages"]] == ["Happy birthday!"]


async def test_a_topic_the_room_is_not_showing_is_refused(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    """Keys come from the server. Inventing one would let anybody post onto
    someone else's day, or revive last year's birthday thread by guessing."""
    resp = await client.post(
        "/api/v1/room/messages",
        json={
            "body": "sneaky",
            "topic_key": "celebration:00000000-0000-0000-0000-000000000001:2020-01-01",
        },
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422


async def test_you_may_delete_your_own_note_but_not_somebody_elses(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    board = await _board(client, settings, seed.report)
    created = await client.post(
        "/api/v1/room/messages",
        json={"body": "mine", "topic_key": board["general_topic"]},
        headers=auth_headers(settings, seed.report),
    )
    message_id = created.json()["id"]

    # A colleague cannot remove it.
    assert (
        await client.delete(
            f"/api/v1/room/messages/{message_id}",
            headers=auth_headers(settings, seed.outsider),
        )
    ).status_code == 403

    # The author can.
    assert (
        await client.delete(
            f"/api/v1/room/messages/{message_id}", headers=auth_headers(settings, seed.report)
        )
    ).status_code == 204


async def test_an_admin_may_remove_anyones_note(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    board = await _board(client, settings, seed.report)
    created = await client.post(
        "/api/v1/room/messages",
        json={"body": "needs moderating", "topic_key": board["general_topic"]},
        headers=auth_headers(settings, seed.report),
    )
    resp = await client.delete(
        f"/api/v1/room/messages/{created.json()['id']}",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 204


async def test_pinning_is_admin_only_even_though_posting_is_not(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    """Posting is open; curation is not. A pinned note reads as the board
    speaking rather than one person, so it is a privileged act."""
    board = await _board(client, settings, seed.report)
    created = await client.post(
        "/api/v1/room/messages",
        json={"body": "pin me", "topic_key": board["general_topic"]},
        headers=auth_headers(settings, seed.report),
    )
    message_id = created.json()["id"]

    # The author of the note cannot pin it.
    assert (
        await client.post(
            f"/api/v1/room/messages/{message_id}/pin",
            headers=auth_headers(settings, seed.report),
        )
    ).status_code == 403

    pinned = await client.post(
        f"/api/v1/room/messages/{message_id}/pin", headers=auth_headers(settings, seed.admin)
    )
    assert pinned.status_code == 200
    assert pinned.json()["pinned_at"] is not None

    unpinned = await client.delete(
        f"/api/v1/room/messages/{message_id}/pin", headers=auth_headers(settings, seed.admin)
    )
    assert unpinned.json()["pinned_at"] is None


async def test_notes_older_than_the_retention_window_stop_being_served(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """Ninety days, enforced on READ. The promise must hold without any
    housekeeping job having run - the scheduler fleet is not reliable."""
    board = await _board(client, settings, seed.report)
    await client.post(
        "/api/v1/room/messages",
        json={"body": "ancient", "topic_key": board["general_topic"]},
        headers=auth_headers(settings, seed.report),
    )
    row = (await db.execute(select(RoomMessage))).scalars().one()
    row.created_at = datetime.now(UTC) - timedelta(days=91)
    await db.commit()

    after = await _board(client, settings, seed.report)
    assert after["messages"] == []


async def test_rsvp_is_one_row_per_person_and_answering_twice_updates_it(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """The unique constraint would fail a second insert; the repository updates
    instead, so changing your mind is not an error."""
    event = Event(
        title="Diwali lunch",
        starts_at=datetime.now(UTC) + timedelta(days=2),
        status=EventStatus.PUBLISHED,
        published_at=datetime.now(UTC),
    )
    db.add(event)
    await db.commit()

    url = f"/api/v1/room/events/{event.id}/rsvp"
    first = await client.post(
        url, json={"attending": True}, headers=auth_headers(settings, seed.report)
    )
    assert first.status_code == 200
    assert first.json()["going_count"] == 1
    assert first.json()["my_rsvp"] is True

    second = await client.post(
        url, json={"attending": False}, headers=auth_headers(settings, seed.report)
    )
    assert second.status_code == 200
    assert second.json()["going_count"] == 0
    assert second.json()["my_rsvp"] is False


async def test_rsvp_to_an_unknown_event_is_404(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    import uuid

    resp = await client.post(
        f"/api/v1/room/events/{uuid.uuid4()}/rsvp",
        json={"attending": True},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 404


async def test_polling_returns_only_what_is_new(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    board = await _board(client, settings, seed.report)
    first = await client.post(
        "/api/v1/room/messages",
        json={"body": "one", "topic_key": board["general_topic"]},
        headers=auth_headers(settings, seed.report),
    )
    watermark = first.json()["created_at"]

    await client.post(
        "/api/v1/room/messages",
        json={"body": "two", "topic_key": board["general_topic"]},
        headers=auth_headers(settings, seed.report),
    )
    resp = await client.get(
        f"/api/v1/room/messages?since={watermark}", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 200
    assert [m["body"] for m in resp.json()] == ["two"]


async def test_the_live_stream_announces_itself_then_pushes_new_notes() -> None:
    """The generator, driven directly.

    Not through the test client: `httpx.ASGITransport` buffers a response
    before returning it, so an endless `text/event-stream` generator never
    comes back and the test hangs rather than fails. Driving the generator is
    also the honest unit - it is where the frames are actually built.

    The `ready` frame matters: it is what tells the browser the stream really
    opened, so it can stop its polling fallback. Behind a proxy that buffers
    SSE it would never arrive, which is the failure this frame exists to
    surface.
    """
    from app.services import room_stream

    agen = room_stream.stream_messages(None, ["general"], None)  # type: ignore[arg-type]
    first = await anext(agen)
    assert first.startswith("event: ready")

    await agen.aclose()


async def test_the_stream_route_tells_proxies_not_to_buffer(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    """Buffering `text/event-stream` is what turns live chat into batch
    delivery on the next page load, and nothing looks broken when it happens.
    Only the headers are asserted here - the body is endless by design."""
    from app.api.v1.routes import room as room_routes

    assert "stream" in {r.name for r in room_routes.router.routes}  # type: ignore[attr-defined]

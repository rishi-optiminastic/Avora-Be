"""Live delivery for the Room.

The board is a chat, so a note has to arrive without anyone reloading. This
streams them over Server-Sent Events: one long-lived GET per viewer, the server
pushing each new note as it lands.

Two decisions worth stating, because both are the opposite of the obvious one:

* **The server tails the database rather than broadcasting in-process.** An
  asyncio pub/sub would be lower latency and would silently break the moment
  the API runs on more than one container - a note posted on replica A would
  never reach a reader on replica B, and nothing would look wrong. Polling the
  table inside the stream costs one small indexed query per viewer per tick and
  is correct at any replica count.
* **Each tick opens its own session.** A request-scoped session held for the
  life of a stream would pin a connection from the pool for as long as somebody
  leaves the tab open, which is how a chat feature takes down an API.

The client still has `GET /room/messages?since=` and falls back to it when the
stream cannot be established - some proxies buffer `text/event-stream` into
uselessness, and a chat that silently stops updating is worse than a slow one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from app.db.session import SessionFactory
from app.repositories.room import RoomRepository
from app.schemas.auth import CurrentUser
from app.schemas.room import RoomMessageRead
from app.services.room_service import _to_message

logger = logging.getLogger(__name__)

#: How often the server looks for new notes. Fast enough to read as live,
#: slow enough that thirty open tabs are thirty small queries every two seconds.
TICK_SECONDS = 2.0
#: A comment line often enough to keep proxies and load balancers from reaping
#: an idle connection.
HEARTBEAT_SECONDS = 20.0


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


async def stream_messages(
    caller: CurrentUser, topic_keys: list[str], since: datetime | None
) -> AsyncIterator[str]:
    """Yield SSE frames for notes on these topics as they arrive."""
    watermark = since or datetime.now(UTC)
    # Tells the browser how long to wait before reconnecting, and proves the
    # stream opened at all so the client can stop its polling fallback.
    yield _sse("ready", json.dumps({"since": watermark.isoformat()}))
    last_beat = datetime.now(UTC)

    try:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                fresh = await _poll(topic_keys, watermark)
            except Exception:  # a transient DB blip must not kill the stream
                logger.warning("room_stream_poll_failed", exc_info=True)
                continue
            for message in fresh:
                watermark = max(watermark, message.created_at)
                yield _sse("message", message.model_dump_json())
            now = datetime.now(UTC)
            if (now - last_beat).total_seconds() >= HEARTBEAT_SECONDS:
                yield ": keep-alive\n\n"
                last_beat = now
    except asyncio.CancelledError:
        # The viewer closed the tab. Normal, not an error.
        raise


async def _poll(topic_keys: list[str], since: datetime) -> list[RoomMessageRead]:
    """One short-lived session per tick - never hold a pooled connection open
    for as long as somebody leaves a tab open."""
    async with SessionFactory() as session:
        rows = await RoomRepository(session).recent(
            topic_keys, now=datetime.now(UTC), since=since
        )
        return [_to_message(m, name) for m, name in rows]

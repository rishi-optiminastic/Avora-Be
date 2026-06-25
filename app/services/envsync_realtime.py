"""In-process WebSocket fan-out for Env Sync live updates.

Best-effort: a push notifies every socket currently subscribed to the project so
a dashboard/collaborator edit appears in the extension instantly instead of on
the next poll. The payload deliberately omits `content` — clients re-fetch it
over the authenticated REST endpoint rather than trusting the socket.

NOTE: single-instance only (an in-memory registry). For a multi-instance
deployment, back this with Redis pub/sub. The extension's 60s poll is the
reliable fallback when a socket is down, so this is an enhancement, not a
dependency.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from app.core.logging import get_logger

logger = get_logger("app.envsync.realtime")


class EnvRealtimeHub:
    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[WebSocket]] = defaultdict(set)

    def add(self, project_id: uuid.UUID, socket: WebSocket) -> None:
        self._subscribers[project_id].add(socket)

    def remove(self, project_id: uuid.UUID, socket: WebSocket) -> None:
        subs = self._subscribers.get(project_id)
        if subs is None:
            return
        subs.discard(socket)
        if not subs:
            self._subscribers.pop(project_id, None)

    async def broadcast(self, project_id: uuid.UUID, message: dict[str, Any]) -> None:
        subs = list(self._subscribers.get(project_id, ()))
        for socket in subs:
            try:
                await socket.send_json(message)
            except Exception:
                self.remove(project_id, socket)


# Module-level singleton — shared by the push path and the WS endpoint.
hub = EnvRealtimeHub()

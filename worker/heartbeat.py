"""Best-effort liveness heartbeat for the scheduler workers (Better Stack et al.).

Each scheduler calls `await beat("HEARTBEAT_URL_EOD")` after a *successful* tick.
If the env var is unset it's a no-op — heartbeats are opt-in per deploy. The ping
runs in a thread with a short timeout and swallows every error: monitoring must
never block a worker's loop or take it down. A missed ping (container down, loop
wedged) is what surfaces in Better Stack as a down alert — exactly the failure
that delivered an EOD report the next morning instead of at the send time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.request

log = logging.getLogger("heartbeat")


def _ping(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 (operator-set URL)
            resp.read(64)
    except Exception as exc:  # never let monitoring break the worker
        log.warning("heartbeat ping failed: %s", exc)


async def beat(env_var: str) -> None:
    """Ping the heartbeat URL named by `env_var`, if it's set. Off by default."""
    url = os.getenv(env_var, "").strip()
    if not url:
        return
    await asyncio.to_thread(_ping, url)

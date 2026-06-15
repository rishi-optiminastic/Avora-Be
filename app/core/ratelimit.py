"""Minimal in-process rate limiter (fixed window per key).

Sufficient for a single-instance dev/staging deploy. For multi-instance
production, back this with Redis — the interface stays the same.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class FixedWindowRateLimiter:
    def __init__(self, *, max_requests: int, window_seconds: int = 60) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._lock = threading.Lock()
        # key -> (window_start_epoch, count)
        self._buckets: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            start, count = self._buckets[key]
            if now - start >= self._window:
                self._buckets[key] = (now, 1)
                return True
            if count >= self._max:
                return False
            self._buckets[key] = (start, count + 1)
            return True

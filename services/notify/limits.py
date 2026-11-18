"""Small in-process limits for public subscribe and feed creation."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, limit: int = 10, window_seconds: int = 60):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        with self._lock:
            recent = [stamp for stamp in self._hits.get(key, []) if moment - stamp < self.window]
            if len(recent) >= self.limit:
                self._hits[key] = recent
                return False
            recent.append(moment)
            self._hits[key] = recent
            return True

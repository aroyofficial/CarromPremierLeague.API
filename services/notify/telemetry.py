"""Counters for match alerts and calendar sync.

Counts live in this process and reset on restart. The durable record is
tblNotificationDeliveries and tblNotificationOutbox; these counters are for a
quick look at what the worker is doing right now. Only sanitized categories are
used as labels. Never put a token, endpoint, or provider body in a label.
"""

from __future__ import annotations

import threading
from collections import Counter

_lock = threading.Lock()
_counts: Counter[str] = Counter()

ALLOWED_CHANNELS = {"push", "google", "microsoft"}


def count(name: str, channel: str | None = None, category: str | None = None) -> None:
    key = name
    if channel:
        key = f"{key}.{channel if channel in ALLOWED_CHANNELS else 'other'}"
    if category:
        key = f"{key}.{_clean(category)}"
    with _lock:
        _counts[key] += 1


def _clean(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value)[:48] or "unknown"


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(sorted(_counts.items()))


def reset() -> None:
    with _lock:
        _counts.clear()

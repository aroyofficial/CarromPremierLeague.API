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
_latency: dict[str, list[float]] = {}

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


def observe(name: str, seconds: float) -> None:
    # Count, total, and worst case for one measure. The name is a fixed constant in the code, never input.
    with _lock:
        entry = _latency.setdefault(name, [0, 0.0, 0.0])
        entry[0] += 1
        entry[1] += max(seconds, 0.0)
        entry[2] = max(entry[2], max(seconds, 0.0))


def latency() -> dict[str, dict[str, float]]:
    with _lock:
        return {name: {"count": entry[0], "average_seconds": round(entry[1] / entry[0], 1) if entry[0] else 0.0, "max_seconds": round(entry[2], 1)} for name, entry in sorted(_latency.items())}


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(sorted(_counts.items()))


def reset() -> None:
    with _lock:
        _counts.clear()
        _latency.clear()

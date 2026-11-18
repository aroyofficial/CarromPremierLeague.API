"""Retry timing for outbox rows.

A transient failure waits longer after each attempt, up to an hour, with about
20% jitter so several rows that failed together do not retry together. A
provider's Retry-After wins when it asks for longer. After MAX_ATTEMPTS the row
is dead.
"""

from __future__ import annotations

import random
from typing import Callable

MAX_ATTEMPTS = 8
BASE_SECONDS = 60
CAP_SECONDS = 60 * 60
MAX_RETRY_AFTER_SECONDS = 6 * 60 * 60


def retry_delay_seconds(
    attempts: int,
    retry_after: int | None = None,
    rand: Callable[[], float] = random.random,
) -> int:
    exponent = min(max(attempts, 1) - 1, 10)
    base = min(CAP_SECONDS, BASE_SECONDS * (2**exponent))
    jittered = base * (0.8 + 0.4 * rand())
    delay = int(min(CAP_SECONDS, jittered))
    if retry_after and retry_after > 0:
        delay = max(delay, min(int(retry_after), MAX_RETRY_AFTER_SECONDS))
    return delay

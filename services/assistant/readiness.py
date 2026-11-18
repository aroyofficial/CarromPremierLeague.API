"""Whether any configured provider can answer right now.

A short probe learns that a key is missing, rejected, or out of credits. The
result is remembered so page loads do not call a model every time. A brief
rate limit still counts as usable: the credits are there, the provider is busy.
"""

import time

from services.assistant.providers import PROBE_TIMEOUT, ProviderError, configured_providers, request_timeout

USABLE = {"ok", "limited"}
_TTL = {"ok": 600, "limited": 45, "down": 45, "exhausted": 120, "unavailable": 120}
_cache: dict[str, tuple[str, float]] = {}


def clear() -> None:
    _cache.clear()


def remember(name: str, kind: str) -> None:
    _cache[name] = (kind, time.monotonic())


def cached(name: str) -> str | None:
    item = _cache.get(name)
    if item is None:
        return None
    kind, seen = item
    if time.monotonic() - seen > _TTL.get(kind, 45):
        return None
    return kind


async def probe(provider) -> str:
    token = request_timeout.set(PROBE_TIMEOUT)
    try:
        check = getattr(provider, "check", None)
        if check is not None:
            await check()
        else:
            await provider.session("Reply with ok.", [], []).step(allow_tools=False)
        return "ok"
    except ProviderError as error:
        return error.kind
    except Exception:
        return "down"
    finally:
        request_timeout.reset(token)


async def usable_labels(client) -> list[str]:
    labels = []
    for provider in configured_providers(client):
        kind = cached(provider.name)
        if kind is None:
            kind = await probe(provider)
            remember(provider.name, kind)
        if kind in USABLE:
            labels.append(provider.label)
    return labels

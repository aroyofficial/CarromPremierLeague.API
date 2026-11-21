"""In-process change notices for the web app.

One uvicorn process delivers these. A second process would not see these subscribers.
Replace MemoryHub when more than one process serves traffic. Nothing is published
unless CPL_REALTIME_ENABLED=true.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

MAX_SUBSCRIBERS = 200
QUEUE_SIZE = 32
TICKET_SECONDS = 60
MAX_TICKETS = 1000

PUBLIC_CHANNEL = "public"
# Admin-only notices. Only ticketed admin streams subscribe, never the public stream.
STAFF_CHANNEL = "staff"
# First path segment of a successful write -> the area clients refetch.
PUBLIC_AREAS = frozenset({"matches", "seasons", "teams", "players", "countries", "rosters", "settings", "gallery", "widgets"})
STAFF_SEGMENTS = frozenset({"admins", "auth", "apps", "webhooks", "widgets"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class HubFull(Exception):
    pass


@dataclass(frozen=True)
class RealtimeEvent:
    event_id: str
    event_type: str
    event_version: int
    occurred_at_utc: str
    entity_type: str
    entity_id: str
    payload: dict

    def to_json(self) -> str:
        return json.dumps({
            "eventId": self.event_id,
            "eventType": self.event_type,
            "eventVersion": self.event_version,
            "occurredAtUtc": self.occurred_at_utc,
            "entityType": self.entity_type,
            "entityId": self.entity_id,
            "correlationId": None,
            "payload": self.payload,
        }, separators=(",", ":"))


def _event(event_type: str, entity_type: str, entity_id: str, payload: dict) -> RealtimeEvent:
    return RealtimeEvent(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        event_version=1,
        occurred_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload,
    )


def league_invalidated(area: str) -> RealtimeEvent:
    return _event("league.invalidated", "area", area, {"area": area})


def admin_invalidated(area: str) -> RealtimeEvent:
    return _event("admin.invalidated", "area", area, {"area": area})


def login_request_changed(admin_id: int) -> RealtimeEvent:
    return _event("admin.login_request.changed", "admin", str(int(admin_id)), {})


def admin_channel(admin_id: int) -> str:
    return f"admin:{int(admin_id)}"


def _segments(path: str) -> list[str]:
    return [part for part in path.strip("/").lower().split("/") if part]


def public_area(method: str, path: str) -> str | None:
    if method.upper() not in WRITE_METHODS:
        return None
    segments = _segments(path)
    if not segments:
        return None
    if "gallery" in segments or "gallery-cover" in segments:
        return "gallery"
    return segments[0] if segments[0] in PUBLIC_AREAS else None


def staff_notices(method: str, path: str, actor_id: int | None) -> list[tuple[str, str]]:
    """Channels and areas for a successful admin-account write: every admin console, the actor, the target."""
    if method.upper() not in WRITE_METHODS:
        return []
    segments = _segments(path)
    if not segments or segments[0] not in STAFF_SEGMENTS:
        return []
    if segments[0] in {"apps", "webhooks", "widgets"}:
        return [(STAFF_CHANNEL, "integrations")]
    notices = [(STAFF_CHANNEL, "admins")]
    targets = []
    if actor_id is not None:
        targets.append(int(actor_id))
    if segments[0] == "admins" and len(segments) > 1 and segments[1].isdigit():
        targets.append(int(segments[1]))
    for target in dict.fromkeys(targets):
        notices.append((admin_channel(target), "me"))
    return notices


class MemoryHub:
    def __init__(self, max_subscribers: int = MAX_SUBSCRIBERS):
        self.max_subscribers = max_subscribers
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._queues: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.published = 0
        self.dropped = 0
        self.rejected = 0

    def subscriber_count(self, channel: str | None = None) -> int:
        if channel is not None:
            return len(self._subscribers.get(channel, ()))
        return len(self._queues)

    async def subscribe(self, channels: str | tuple[str, ...]) -> asyncio.Queue:
        self._loop = asyncio.get_running_loop()
        if self.subscriber_count() >= self.max_subscribers:
            self.rejected += 1
            raise HubFull()
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._queues.add(queue)
        for channel in _as_channels(channels):
            self._subscribers.setdefault(channel, set()).add(queue)
        return queue

    def unsubscribe(self, channels: str | tuple[str, ...], queue: asyncio.Queue) -> None:
        self._queues.discard(queue)
        for channel in _as_channels(channels):
            queues = self._subscribers.get(channel)
            if not queues:
                continue
            queues.discard(queue)
            if not queues:
                self._subscribers.pop(channel, None)

    def publish(self, channel: str, event: RealtimeEvent) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        body = event.to_json()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._enqueue(channel, body)
        else:
            loop.call_soon_threadsafe(self._enqueue, channel, body)
        self.published += 1

    def _enqueue(self, channel: str, body: str) -> None:
        for queue in list(self._subscribers.get(channel, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self.dropped += 1
            queue.put_nowait(body)


def _as_channels(channels: str | tuple[str, ...]) -> tuple[str, ...]:
    return (channels,) if isinstance(channels, str) else tuple(channels)


hub = MemoryHub()


@dataclass(frozen=True)
class Ticket:
    admin_id: int
    username: str
    token_version: int
    token_expires_at: int


class TicketStore:
    """Single-use, short-lived tickets so a browser EventSource never carries the bearer token."""

    def __init__(self, ttl_seconds: int = TICKET_SECONDS, limit: int = MAX_TICKETS):
        self.ttl_seconds = ttl_seconds
        self.limit = limit
        self._items: dict[str, tuple[Ticket, float]] = {}
        self._lock = threading.Lock()

    def issue(self, ticket: Ticket, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        value = secrets.token_urlsafe(32)
        with self._lock:
            self._purge(now)
            if len(self._items) >= self.limit:
                raise HubFull()
            self._items[value] = (ticket, now + self.ttl_seconds)
        return value

    def consume(self, value: str, now: float | None = None) -> Ticket | None:
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._items.pop(value or "", None)
        if entry is None or entry[1] <= now:
            return None
        return entry[0]

    def _purge(self, now: float) -> None:
        for key in [key for key, (_ticket, expires) in self._items.items() if expires <= now]:
            self._items.pop(key, None)


tickets = TicketStore()


def realtime_enabled() -> bool:
    return os.getenv("CPL_REALTIME_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def origin_allowed(origin: str | None, allowed_hosts: list[str]) -> bool:
    if not origin:
        return True
    allowed = {host.strip().rstrip("/") for host in allowed_hosts if host.strip()}
    return origin.strip().rstrip("/") in allowed


def publish_public_change(area: str | None) -> None:
    if not area or not realtime_enabled():
        return
    hub.publish(PUBLIC_CHANNEL, league_invalidated(area))


def publish_admin_change(channel: str, area: str) -> None:
    if not realtime_enabled():
        return
    hub.publish(channel, admin_invalidated(area))


def publish_login_request_changed(admin_id: int | None) -> None:
    if admin_id is None or not realtime_enabled():
        return
    hub.publish(admin_channel(admin_id), login_request_changed(admin_id))


def format_sse(body: str) -> str:
    event = json.loads(body)
    return f"id: {event['eventId']}\nevent: {event['eventType']}\ndata: {body}\n\n"


def _actor_id(scope) -> int | None:
    from core.security import decode_access_token

    for name, value in scope.get("headers") or ():
        if name.lower() != b"authorization":
            continue
        scheme, _, token = value.decode("latin-1").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        try:
            return int(decode_access_token(token.strip())["sub"])
        except Exception:
            return None
    return None


class PublicChangeMiddleware:
    """After a successful write, tell open browsers to refetch what changed.

    League writes go to the public channel. Admin-account writes go to the staff channel and to the
    acting and target admins. Sync route handlers commit before the response starts, so this runs
    after the change is stored.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope.get("method", ""), scope.get("path", "")
        area = public_area(method, path)
        staff = staff_notices(method, path, None)
        if area is None and not staff:
            await self.app(scope, receive, send)
            return
        if staff:
            staff = staff_notices(method, path, _actor_id(scope))

        async def watch(message):
            if message.get("type") == "http.response.start" and 200 <= int(message.get("status", 0)) < 300:
                if area is not None:
                    publish_public_change(area)
                for channel, notice_area in staff:
                    publish_admin_change(channel, notice_area)
            await send(message)

        await self.app(scope, receive, watch)

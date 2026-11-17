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
# First path segment of a successful write -> the area clients refetch.
PUBLIC_AREAS = frozenset({"matches", "seasons", "teams", "players", "countries", "rosters", "settings"})


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


def login_request_changed(admin_id: int) -> RealtimeEvent:
    return _event("admin.login_request.changed", "admin", str(int(admin_id)), {})


def admin_channel(admin_id: int) -> str:
    return f"admin:{int(admin_id)}"


def public_area(method: str, path: str) -> str | None:
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    segment = path.strip("/").split("/", 1)[0].lower()
    return segment if segment in PUBLIC_AREAS else None


class MemoryHub:
    def __init__(self, max_subscribers: int = MAX_SUBSCRIBERS):
        self.max_subscribers = max_subscribers
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self.published = 0
        self.dropped = 0
        self.rejected = 0

    def subscriber_count(self, channel: str | None = None) -> int:
        if channel is not None:
            return len(self._subscribers.get(channel, ()))
        return sum(len(queues) for queues in self._subscribers.values())

    async def subscribe(self, channel: str) -> asyncio.Queue:
        self._loop = asyncio.get_running_loop()
        if self.subscriber_count() >= self.max_subscribers:
            self.rejected += 1
            raise HubFull()
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.setdefault(channel, set()).add(queue)
        return queue

    def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(channel)
        if not queues:
            return
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


def publish_login_request_changed(admin_id: int | None) -> None:
    if admin_id is None or not realtime_enabled():
        return
    hub.publish(admin_channel(admin_id), login_request_changed(admin_id))


def format_sse(body: str) -> str:
    event = json.loads(body)
    return f"id: {event['eventId']}\nevent: {event['eventType']}\ndata: {body}\n\n"


class PublicChangeMiddleware:
    """After a successful public write, tell open browsers to refetch that area.

    Sync route handlers commit before the response starts, so this runs after the change is stored.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        area = public_area(scope.get("method", ""), scope.get("path", ""))
        if area is None:
            await self.app(scope, receive, send)
            return

        async def watch(message):
            if message.get("type") == "http.response.start" and 200 <= int(message.get("status", 0)) < 300:
                publish_public_change(area)
            await send(message)

        await self.app(scope, receive, watch)

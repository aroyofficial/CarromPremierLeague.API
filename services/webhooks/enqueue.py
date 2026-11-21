from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from repositories.notify_repository import NotifyRepository
from repositories.webhook_repository import WebhookRepository
from services.notify.events import idempotency_key
from services.webhooks.events import (
    ALL_WEBHOOK_EVENTS,
    build_envelope,
    classify_match_webhook_events,
)

logger = logging.getLogger(__name__)


def webhooks_enabled() -> bool:
    raw = os.getenv("CPL_WEBHOOKS_ENABLED")
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() in {"1", "true", "yes"}


def _usable_db(db) -> bool:
    return db is not None and hasattr(db, "cursor")


def _to_plain(data: Any) -> Any:
    if hasattr(data, "model_dump"):
        return data.model_dump(mode="json")
    return data


def publish_webhook_event(db, event_type: str, data: Any, *, key: str | None = None) -> int:
    """Fan out one event to every active webhook subscribed to `event_type`.

    Never makes external HTTP calls and never raises into a route write.
    """
    if event_type not in ALL_WEBHOOK_EVENTS or not webhooks_enabled() or not _usable_db(db):
        return 0
    try:
        repository = WebhookRepository(db)
        subscribers = repository.active_for_event(event_type)
        if not subscribers:
            return 0
        event_id, payload_json = build_envelope(event_type, _to_plain(data))
        dedupe = key or f"{event_type}:{uuid.uuid4().hex}"
        queued = 0
        for subscriber in subscribers:
            if repository.enqueue_delivery(
                webhook_id=int(subscriber["id"]),
                event_id=event_id,
                event_type=event_type,
                idempotency_key=dedupe,
                payload=payload_json,
            ):
                queued += 1
        return queued
    except Exception:
        logger.debug("Could not queue webhook event %s", event_type, exc_info=True)
        return 0


def load_match_snapshot(db, match_id: int) -> dict | None:
    if not webhooks_enabled() or not _usable_db(db):
        return None
    try:
        return NotifyRepository(db).match_notice(match_id)
    except Exception:
        logger.debug("Could not read match %s for webhook snapshot", match_id, exc_info=True)
        return None


def publish_match_webhooks(
    db,
    kind: str,
    before: dict | None,
    after: dict | None,
    *,
    events: list[str] | None = None,
) -> int:
    if not webhooks_enabled() or not _usable_db(db):
        return 0
    chosen = events if events is not None else classify_match_webhook_events(kind, before, after)
    snapshot = after or before
    if not snapshot or not chosen:
        return 0
    match_id = int(snapshot.get("id") or 0)
    updated_at = snapshot.get("updated_at")
    queued = 0
    for event_type in chosen:
        key = idempotency_key(match_id, event_type, updated_at) if match_id else None
        queued += publish_webhook_event(
            db,
            event_type,
            {"match": snapshot, "before": before},
            key=key,
        )
    return queued


def publish_match_side_effect_webhooks(db, notices: list[tuple[str, int]], already: set[int]) -> None:
    if not webhooks_enabled() or not _usable_db(db):
        return
    for kind, match_id in notices:
        if match_id in already:
            continue
        already.add(match_id)
        after = load_match_snapshot(db, match_id)
        event_type = "match.scheduled" if kind == "created" else "match.teams_changed"
        publish_match_webhooks(db, kind, None, after, events=[event_type])

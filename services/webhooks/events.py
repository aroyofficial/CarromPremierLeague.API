from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from services.notify.events import (
    CANCELLED,
    ENDED,
    LIVE,
    RESCHEDULED,
    RESET,
    SCORE,
    SCHEDULED,
    TEAMS,
    classify,
)


@dataclass(frozen=True)
class WebhookEventDef:
    code: str
    group: str
    name: str
    description: str


WEBHOOK_EVENTS: tuple[WebhookEventDef, ...] = (
    WebhookEventDef("match.scheduled", "Matches", "Match scheduled", "A new fixture is scheduled."),
    WebhookEventDef("match.rescheduled", "Matches", "Match rescheduled", "A fixture's scheduled date changes."),
    WebhookEventDef("match.teams_changed", "Matches", "Match teams changed", "A fixture's teams are assigned or updated."),
    WebhookEventDef("match.live", "Matches", "Match went live", "A match transitions to in-progress."),
    WebhookEventDef("match.ended", "Matches", "Match completed", "A match finishes and its outcome is recorded."),
    WebhookEventDef("match.score_updated", "Matches", "Match score updated", "A played match's outcome, net points or stats change."),
    WebhookEventDef("match.reset", "Matches", "Match reset", "A started or played match returns to scheduled."),
    WebhookEventDef("match.cancelled", "Matches", "Match cancelled", "A fixture is deleted or voided."),
    WebhookEventDef("season.created", "Seasons", "Season created", "A new season is created."),
    WebhookEventDef("season.updated", "Seasons", "Season updated", "Season details or teams are updated."),
    WebhookEventDef("season.schedule_rebuilt", "Seasons", "Schedule rebuilt", "League fixtures are generated or rebuilt for a season."),
    WebhookEventDef("team.created", "League", "Team created", "A new team is added."),
    WebhookEventDef("team.updated", "League", "Team updated", "A team's profile is updated."),
    WebhookEventDef("player.created", "League", "Player created", "A new player is added."),
    WebhookEventDef("player.updated", "League", "Player updated", "A player's profile is updated."),
    WebhookEventDef("roster.updated", "League", "Roster updated", "A player is assigned to or removed from a season roster."),
    WebhookEventDef("settings.updated", "Settings", "Settings updated", "Competition settings are saved."),
)

TEST_EVENT = "webhook.test"
ALL_WEBHOOK_EVENTS: frozenset[str] = frozenset(item.code for item in WEBHOOK_EVENTS)

_NOTIFY_TO_WEBHOOK = {
    SCHEDULED: "match.scheduled",
    RESCHEDULED: "match.rescheduled",
    TEAMS: "match.teams_changed",
    LIVE: "match.live",
    ENDED: "match.ended",
    SCORE: "match.score_updated",
    RESET: "match.reset",
    CANCELLED: "match.cancelled",
}


def ordered_webhook_events(codes) -> list[str]:
    wanted = set(codes)
    return [item.code for item in WEBHOOK_EVENTS if item.code in wanted]


def classify_match_webhook_events(kind: str, before: dict | None, after: dict | None) -> list[str]:
    raw = classify(kind, before, after)
    return [_NOTIFY_TO_WEBHOOK[code] for code in raw if code in _NOTIFY_TO_WEBHOOK]


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def build_envelope(event_type: str, data: Any, *, event_id: str | None = None, occurred_at: datetime | None = None) -> tuple[str, str]:
    eid = event_id or f"evt_{uuid.uuid4().hex}"
    stamp = occurred_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    envelope = {
        "id": eid,
        "event": event_type,
        "occurred_at": stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": data,
    }
    return eid, json.dumps(envelope, default=_json_default, separators=(",", ":"))

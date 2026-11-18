"""Which match changes are worth telling people about.

The league stores a status, a date, two teams, and a result. It does not store a
venue or a start time, and it has no separate postponed status. A date change is
the postpone. A voided or deleted match is the cancellation.
"""

from __future__ import annotations

from datetime import date, datetime


SCHEDULED = "scheduled"
RESCHEDULED = "rescheduled"
TEAMS = "teams"
CANCELLED = "cancelled"
LIVE = "live"
ENDED = "ended"
SCORE = "score"
RESET = "reset"
REMINDER = "reminder"
# Calendar-only: writes a match into one newly connected calendar. Never pushed.
SYNC = "sync"
# Calendar-only: a schedule rebuild added or removed this match. Every connected calendar.
CALENDAR = "calendar"
# Push-only, season level: a schedule rebuild. One alert per device, not one per match.
SCHEDULE = "schedule"

STATUS_SCHEDULED = 1
STATUS_LIVE = 2
STATUS_PLAYED = 3


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def classify(kind: str, before: dict | None, after: dict | None) -> list[str]:
    if kind == "created":
        if after and not after.get("void"):
            return [SCHEDULED]
        return []
    if kind == "deleted":
        return [CANCELLED]
    if kind == "reset":
        return [RESET]
    if kind == "reminder":
        return [REMINDER]
    if not before or not after:
        return []
    if after.get("void") and not before.get("void"):
        return [CANCELLED]

    events: list[str] = []
    if _day(before.get("scheduled_date")) != _day(after.get("scheduled_date")):
        events.append(RESCHEDULED)
    before_teams = (before.get("team1_id"), before.get("team2_id"))
    after_teams = (after.get("team1_id"), after.get("team2_id"))
    if before_teams != after_teams:
        events.append(TEAMS)
    if int(before.get("status") or 0) != int(after.get("status") or 0):
        status = int(after.get("status") or 0)
        if status == STATUS_LIVE:
            events.append(LIVE)
        elif status == STATUS_PLAYED:
            events.append(ENDED)
        elif status == STATUS_SCHEDULED:
            events.append(RESET)
    elif int(after.get("status") or 0) == STATUS_PLAYED and (
        before.get("outcome") != after.get("outcome") or before.get("net_points") != after.get("net_points")
    ):
        events.append(SCORE)
    return events


def idempotency_key(match_id: int, event: str, updated_at) -> str:
    if isinstance(updated_at, datetime):
        stamp = updated_at.strftime("%Y-%m-%dT%H:%M:%S.%f")
    elif isinstance(updated_at, date):
        stamp = updated_at.isoformat()
    else:
        stamp = str(updated_at or "0")
    return f"{int(match_id)}:{event}:{stamp}"

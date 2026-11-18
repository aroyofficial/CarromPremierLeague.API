"""Decide who hears about a match change. No network and no database."""

from __future__ import annotations

from services.notify.events import (
    CALENDAR,
    CANCELLED,
    ENDED,
    LIVE,
    REMINDER,
    RESCHEDULED,
    RESET,
    SCHEDULE,
    SCHEDULED,
    SCORE,
    SYNC,
    TEAMS,
)
from services.notify.flags import NotifyFlags

PREFERENCE_FIELD = {
    SCHEDULED: "notify_scheduled",
    SCHEDULE: "notify_scheduled",
    RESCHEDULED: "notify_changed",
    TEAMS: "notify_changed",
    CANCELLED: "notify_cancelled",
    LIVE: "notify_live",
    ENDED: "notify_ended",
    SCORE: "notify_score",
    RESET: "notify_changed",
    REMINDER: "notify_reminder",
}

CALENDAR_ONLY = {SYNC, CALENDAR}
PUSH_ONLY = {SCHEDULE}
LIVE_STATUS = {LIVE, ENDED, SCORE}


def subscription_wants(subscription: dict, event: str, match: dict) -> bool:
    field = PREFERENCE_FIELD.get(event)
    if field and not subscription.get(field, True):
        return False
    season_id = subscription.get("season_id")
    if season_id not in (None, "", 0) and int(season_id) != int(match.get("season_id") or 0):
        return False
    if event == SCHEDULE:
        return True
    team_id = subscription.get("team_id")
    if team_id not in (None, "", 0):
        teams = {match.get("team1_id"), match.get("team2_id")}
        if int(team_id) not in {int(team) for team in teams if team not in (None, "")}:
            return False
    return True


def delivery_plan(
    event: str,
    match: dict,
    subscriptions: list[dict],
    connections: list[dict],
    flags: NotifyFlags,
    only_connection: int | None = None,
) -> list[dict]:
    actions: list[dict] = []
    if event == SYNC:
        connections = [row for row in connections if only_connection is not None and int(row.get("id") or 0) == int(only_connection)]

    if event not in CALENDAR_ONLY:
        if event == LIVE and flags.push_ready() and not flags.push_live_enabled:
            actions.append({"channel": "push", "status": "skipped", "reason": "CPL_PUSH_LIVE_ENABLED is off."})
        elif flags.push_ready():
            for subscription in subscriptions:
                if subscription.get("void"):
                    continue
                if not subscription_wants(subscription, event, match):
                    continue
                actions.append(
                    {
                        "channel": "push",
                        "status": "send",
                        "target": subscription.get("id"),
                        "subscription": subscription,
                    }
                )
        elif flags.push_enabled:
            actions.append({"channel": "push", "status": "skipped", "reason": flags.push_block_reason()})

    if event in PUSH_ONLY:
        return actions

    for provider, ready, reason in (
        ("google", flags.google_ready(), flags.google_block_reason()),
        ("microsoft", flags.microsoft_ready(), flags.microsoft_block_reason()),
    ):
        linked = [row for row in connections if row.get("provider") == provider and not row.get("void")]
        if not ready:
            if flags.google_enabled if provider == "google" else flags.microsoft_enabled:
                actions.append({"channel": provider, "status": "skipped", "reason": reason})
            continue
        if event in LIVE_STATUS and not flags.calendar_live_updates_enabled:
            if linked:
                actions.append({"channel": provider, "status": "skipped", "reason": "CPL_CALENDAR_LIVE_UPDATES_ENABLED is off."})
            continue
        for connection in linked:
            actions.append(
                {
                    "channel": provider,
                    "status": "send",
                    "target": connection.get("id"),
                    "connection": connection,
                }
            )
    return actions

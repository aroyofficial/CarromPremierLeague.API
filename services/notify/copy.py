"""Short match text shared by push alerts and calendar events."""

from __future__ import annotations

from datetime import date, datetime

from services.notify.events import ENDED, LIVE, REMINDER, RESCHEDULED, SCORE

CATEGORIES = {
    1: "League",
    2: "Final",
    3: "Eliminator",
    4: "Qualifier 1",
    5: "Qualifier 2",
    6: "Friendly",
    7: "Golden Strike",
}


def pair(match: dict) -> str:
    home = match.get("team1_name") or "TBD"
    away = match.get("team2_name") or "TBD"
    return f"{home} vs {away}"


def _day_label(value) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    return str(value or "a later date")


def result_line(match: dict) -> str:
    if int(match.get("status") or 0) != 3:
        return ""
    net = match.get("net_points")
    margin = f" by {net}" if net not in (None, "") else ""
    if int(match.get("outcome") or 3) == 1:
        return f"{match.get('team1_name') or 'Team 1'} won{margin}"
    if int(match.get("outcome") or 3) == 2:
        return f"{match.get('team2_name') or 'Team 2'} won{margin}"
    return "Match drawn"


def summary(match: dict) -> str:
    title = pair(match)
    if match.get("void"):
        return f"Cancelled: {title}"
    if int(match.get("status") or 0) == 2:
        return f"Live: {title}"
    if int(match.get("status") or 0) == 3:
        return result_line(match) or title
    return title


def description(match: dict, public_web_url: str) -> str:
    lines = [
        summary(match),
        f"{match.get('season_name') or 'Season'} · {CATEGORIES.get(int(match.get('category') or 0), 'Match')}",
        "The league records the match date. It does not record a start time or a venue.",
    ]
    scored = result_line(match)
    if scored:
        lines.append(scored)
    season_id = match.get("season_id")
    if public_web_url and season_id:
        lines.append(f"{public_web_url}/schedule")
    return "\n".join(lines)


def alert_title(event: str) -> str:
    titles = {
        "scheduled": "Match scheduled",
        "rescheduled": "Match moved",
        "teams": "Match updated",
        "cancelled": "Match cancelled",
        "live": "Match is live",
        "ended": "Match finished",
        "score": "Result updated",
        "reset": "Match reset",
        "reminder": "Match day",
        "schedule": "Schedule updated",
    }
    return titles.get(event, "Carrom Premier League")


def alert_body(event: str, match: dict, before: dict | None = None) -> str:
    title = pair(match)
    when = _day_label(match.get("scheduled_date"))
    if event == "schedule":
        season = match.get("season_name") or "The season"
        count = int((before or {}).get("schedule_count") or 0)
        if count:
            return f"{season} has a new schedule: {count} matches, starting {when}."
        return f"{season} has a new schedule."
    if event == RESCHEDULED and before and before.get("scheduled_date"):
        return f"{title} moved from {_day_label(before.get('scheduled_date'))} to {when}."
    if event == LIVE:
        return f"{title} is live."
    if event in (ENDED, SCORE):
        scored = result_line(match)
        return f"{title}. {scored}." if scored else f"{title} has finished."
    if event == REMINDER:
        return f"{title} is scheduled today, {when}."
    if event == "cancelled":
        return f"{title} on {when} was cancelled."
    if match.get("void"):
        return f"{title} was cancelled."
    return f"{title} · {when}."

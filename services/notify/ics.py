"""RFC 5545 calendars for one match or a whole season.

Dates are all-day because a match row has a date and no kickoff time. SEQUENCE
follows UpdatedAt so a client that polls the feed replaces the event when the
match moves, starts, finishes, or is cancelled.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone

from services.notify.copy import description, summary

PRODID = "-//Carrom Premier League//Match calendar//EN"
UID_HOST = "carrompremierleague"


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _day(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    return date.fromisoformat(text)


def _stamp(value: datetime) -> str:
    return _utc(value).strftime("%Y%m%dT%H%M%SZ")


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def _fold(line: str) -> str:
    # Fold on octets, not characters, so a non-ASCII name cannot make a line too long.
    # A continuation line starts with one space, so later pieces stop at 74 octets.
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts: list[bytes] = []
    limit = 75
    while raw:
        cut = min(limit, len(raw))
        if cut < len(raw):
            while cut > 0 and (raw[cut] & 0xC0) == 0x80:
                cut -= 1
            if cut == 0:
                cut = min(limit, len(raw))
        parts.append(raw[:cut])
        raw = raw[cut:]
        limit = 74
    return "\r\n ".join(part.decode("utf-8") for part in parts)


def sequence_for(match: dict) -> int:
    updated = match.get("updated_at")
    if isinstance(updated, datetime):
        return int(_utc(updated).timestamp())
    if isinstance(updated, str) and updated:
        try:
            parsed = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            return 1
        return int(_utc(parsed).timestamp())
    return 1


def _modified(match: dict) -> datetime | None:
    updated = match.get("updated_at")
    if isinstance(updated, datetime):
        return _utc(updated)
    if isinstance(updated, str) and updated:
        try:
            return _utc(datetime.fromisoformat(updated.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def event_uid(match_id: int) -> str:
    return f"match-{int(match_id)}@{UID_HOST}"


def _alarm(day: date, title: str) -> list[str]:
    # 09:00 in Asia/Kolkata is 03:30 UTC. The league has no start time to count back from.
    moment = datetime.combine(day, time(3, 30), tzinfo=timezone.utc)
    return [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        _fold("DESCRIPTION:" + _escape(f"Match day: {title}")),
        f"TRIGGER;VALUE=DATE-TIME:{_stamp(moment)}",
        "END:VALARM",
    ]


def event_lines(match: dict, public_web_url: str, now: datetime | None = None) -> list[str]:
    day = _day(match.get("scheduled_date"))
    if day is None:
        raise ValueError("A calendar event needs a match date.")
    end = day + timedelta(days=1)
    cancelled = bool(match.get("void"))
    title = summary(match)
    lines = [
        "BEGIN:VEVENT",
        f"UID:{event_uid(int(match['id']))}",
        f"DTSTAMP:{_stamp(now or datetime.now(timezone.utc))}",
        f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
        f"SEQUENCE:{sequence_for(match)}",
    ]
    modified = _modified(match)
    if modified:
        lines.append(f"LAST-MODIFIED:{_stamp(modified)}")
    lines += [
        "STATUS:CANCELLED" if cancelled else "STATUS:CONFIRMED",
        _fold("SUMMARY:" + _escape(title)),
        _fold("DESCRIPTION:" + _escape(description(match, public_web_url))),
        "TRANSP:TRANSPARENT",
    ]
    if public_web_url:
        lines.append(_fold(f"URL:{public_web_url}/schedule"))
    if not cancelled:
        lines.extend(_alarm(day, title))
    lines.append("END:VEVENT")
    return lines


def calendar_text(matches: list[dict], calendar_name: str, public_web_url: str, now: datetime | None = None) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold("X-WR-CALNAME:" + _escape(calendar_name)),
        "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
        "X-PUBLISHED-TTL:PT15M",
    ]
    for match in matches:
        lines.extend(event_lines(match, public_web_url, now=now))
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def calendar_etag(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def not_modified(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    presented = {part.strip().strip('"').strip("W/") for part in if_none_match.split(",")}
    return etag in presented or "*" in presented

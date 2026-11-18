"""Queue match changes after the match write has succeeded.

External calls stay out of this function. If the outbox table is missing, or
the flags are off, the match request still succeeds.
"""

from __future__ import annotations

import logging

from repositories.notify_repository import NotifyRepository
from services.notify.events import CALENDAR, SCHEDULE, SYNC, classify, idempotency_key
from services.notify.flags import load_flags

logger = logging.getLogger(__name__)


def publish_match_change(db, kind: str, before: dict | None, after: dict | None, events: list[str] | None = None) -> None:
    try:
        flags = load_flags()
        if not flags.delivery_ready():
            return
        chosen = events if events is not None else classify(kind, before, after)
        match = after or before
        if not match or not chosen:
            return
        repository = NotifyRepository(db)
        for event in chosen:
            repository.enqueue(match, event, before=before)
    except Exception:
        logger.exception("Could not queue a match notice for match %s", (after or before or {}).get("id"))


def backfill_connection(db, connection_id: int | None, day) -> int:
    """Queue every upcoming match for one newly connected calendar. Returns how many were queued."""
    if not connection_id:
        return 0
    try:
        repository = NotifyRepository(db)
        queued = 0
        for match in repository.upcoming_matches(day):
            key = f"{match['id']}:{SYNC}:{int(connection_id)}"
            if repository.enqueue(match, SYNC, key=key, connection_id=int(connection_id)):
                queued += 1
        return queued
    except Exception:
        logger.exception("Could not queue the calendar backfill for connection %s", connection_id)
        return 0


def season_scheduled(db, season_id: int) -> list[dict] | None:
    """Unplayed matches before a schedule rebuild. None when nothing will be published."""
    try:
        if not load_flags().delivery_ready():
            return None
        return NotifyRepository(db).season_scheduled(season_id)
    except Exception:
        logger.exception("Could not read season %s before a schedule rebuild", season_id)
        return None


def publish_schedule_rebuild(db, season_id: int, removed: list[dict] | None) -> int:
    """A rebuild deletes unplayed matches and inserts new ones with new ids.

    Each removed match is withdrawn from connected calendars and each new one is
    written to them. Devices get one season-level alert rather than one per match.
    Returns how many notices were queued.
    """
    if removed is None:
        return 0
    try:
        repository = NotifyRepository(db)
        removed_ids = {int(match["id"]) for match in removed}
        added = [match for match in repository.season_scheduled(season_id) if int(match["id"]) not in removed_ids]
        queued = 0
        for match in removed:
            gone = dict(match, void=True)
            if repository.enqueue(gone, CALENDAR, key=f"{int(match['id'])}:{CALENDAR}:removed"):
                queued += 1
        for match in added:
            if repository.enqueue(match, CALENDAR, key=f"{int(match['id'])}:{CALENDAR}:added"):
                queued += 1
        if added:
            first = added[0]
            key = f"season:{int(season_id)}:{SCHEDULE}:{int(first['id'])}"
            if repository.enqueue(first, SCHEDULE, before={"schedule_count": len(added)}, key=key):
                queued += 1
        return queued
    except Exception:
        logger.exception("Could not queue the schedule rebuild notices for season %s", season_id)
        return 0


def read_name(db, kind: str, entity_id: int) -> str | None:
    """A team's or season's current name, read before its rename. None when the flags are off."""
    try:
        if not load_flags().delivery_ready():
            return None
        return NotifyRepository(db).entity_name(kind, entity_id)
    except Exception:
        logger.exception("Could not read the %s %s before a rename", kind, entity_id)
        return None


def publish_rename(db, *, team_id: int | None = None, season_id: int | None = None) -> int:
    """A team or season was renamed. Its live matches show the new name in connected calendars
    and in the subscription feed, so each one gets a calendar-only write. Devices get no alert.
    Returns how many notices were queued.
    """
    try:
        if not load_flags().delivery_ready():
            return 0
        repository = NotifyRepository(db)
        queued = 0
        for match in repository.touch_matches_for_rename(team_id=team_id, season_id=season_id):
            key = idempotency_key(int(match["id"]), f"{CALENDAR}:rename", match.get("updated_at"))
            if repository.enqueue(match, CALENDAR, key=key, existing_only=True):
                queued += 1
        return queued
    except Exception:
        logger.exception("Could not queue the rename notices for team %s season %s", team_id, season_id)
        return 0


def publish_player_change(db, *, player_id: int, season_id: int | None = None) -> int:
    """A player was renamed, or added to, moved on, or removed from a roster. Each match the
    player's name appears in gets a calendar-only write, so the event's player list is current.
    Devices get no alert. Returns how many notices were queued.
    """
    try:
        if not load_flags().delivery_ready():
            return 0
        repository = NotifyRepository(db)
        queued = 0
        for match in repository.touch_matches_for_player(int(player_id), season_id=season_id):
            key = idempotency_key(int(match["id"]), f"{CALENDAR}:roster", match.get("updated_at"))
            if repository.enqueue(match, CALENDAR, key=key, existing_only=True):
                queued += 1
        return queued
    except Exception:
        logger.exception("Could not queue the player notices for player %s season %s", player_id, season_id)
        return 0


def load_notice(db, match_id: int) -> dict | None:
    try:
        return NotifyRepository(db).match_notice(match_id)
    except Exception:
        logger.exception("Could not read match %s for a notice", match_id)
        return None


def publish_side_effects(db, notices: list[tuple[str, int]], already: set[int]) -> None:
    for kind, match_id in notices:
        if match_id in already:
            continue
        already.add(match_id)
        after = load_notice(db, match_id)
        event = "scheduled" if kind == "created" else "teams"
        publish_match_change(db, kind, None, after, events=[event])

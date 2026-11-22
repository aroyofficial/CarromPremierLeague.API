from datetime import date, datetime, timedelta, timezone

from enums.match_category import MatchCategory
from enums.match_outcome import MatchOutcome
from enums.match_status import MatchStatus
from fastapi import HTTPException


REGULATION_SECONDS = 15 * 60
EXTRA_SECONDS = 5 * 60
LEAGUE_ZONE = timezone(timedelta(hours=5, minutes=30))

KNOCKOUT_CATEGORIES = frozenset(
    {
        MatchCategory.Eliminator.value,
        MatchCategory.Qualifier1.value,
        MatchCategory.Qualifier2.value,
        MatchCategory.Final.value,
    }
)


def is_knockout(category: int) -> bool:
    return category in KNOCKOUT_CATEGORIES


def league_today() -> date:
    return datetime.now(LEAGUE_ZONE).date()


# One team plays the white coins and the other plays the black coins.
COINS_PER_COLOR = 9

DECISION_EXTRA_TIME = "extra_time"
DECISION_GOLDEN_STRIKE = "golden_strike"


def colour_scores(totals: dict) -> dict:
    """Coins of each team's colour that are off the board.

    `totals` maps a team key to {"own", "opponent", "fines"}. A fine puts one of the team's pocketed
    coins back on the board; a fine the team cannot pay yet is due and returns nothing. Coins the
    other team pocketed of this colour count for this team.
    """
    scores = {}
    for key, mine in totals.items():
        other_taken = sum(int(side["opponent"]) for other, side in totals.items() if other != key)
        scores[key] = max(0, int(mine["own"]) - int(mine["fines"])) + other_taken
    return scores


def coin_line_totals(rows) -> dict:
    """Team totals from scorecard rows read from the database."""
    totals: dict = {}
    for row in rows:
        side = totals.setdefault(int(row["TeamId"]), {"own": 0, "opponent": 0, "fines": 0})
        side["own"] += int(row["CoinsPocketed"] or 0)
        side["opponent"] += int(row["OpponentCoinsPocketed"] or 0)
        side["fines"] += int(row["SingleFines"] or 0) + 2 * int(row["DoubleFines"] or 0)
    return totals


def both_colours_cleared(scores) -> bool:
    """Both teams on 9 at once cannot happen: a colour that is cleared ends the match."""
    return scores is not None and scores[0] >= COINS_PER_COLOR and scores[1] >= COINS_PER_COLOR


def _to_utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def knockout_state(
    category: int,
    status: int,
    scores,
    started_at=None,
    extra_started_at=None,
    golden_strike: bool = False,
    outcome: int | None = None,
    now: datetime | None = None,
    regulation_seconds: int = REGULATION_SECONDS,
    extra_seconds: int = EXTRA_SECONDS,
) -> dict:
    """Where a knockout match stands when it is level.

    `decision` names the question the scorer has to answer now: take the match to extra time
    (regulation is over and the scorecard is level), or go to a Golden Strike (extra time is over
    and the scorecard is still level). `tied` marks a finished knockout match with no winner.
    """
    state = {"level": False, "decision": None, "tied": False}
    if not is_knockout(category):
        return state
    if status == MatchStatus.Played.value:
        state["tied"] = outcome == MatchOutcome.NotDecided.value and not golden_strike
        return state
    if status != MatchStatus.InProgress.value or scores is None:
        return state

    state["level"] = scores[0] == scores[1]
    if golden_strike or not state["level"] or max(scores) >= COINS_PER_COLOR:
        return state

    now = now or datetime.now(timezone.utc)
    extra_start = _to_utc(extra_started_at)
    if extra_start is not None:
        if now >= extra_start + timedelta(seconds=extra_seconds):
            state["decision"] = DECISION_GOLDEN_STRIKE
        return state

    started = _to_utc(started_at)
    if started is not None and now >= started + timedelta(seconds=regulation_seconds):
        state["decision"] = DECISION_EXTRA_TIME
    return state


def _calendar_day(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def validate_lifecycle_update(
    match,
    update_data: dict,
    colour_cleared: bool = False,
    regulation_seconds: int = REGULATION_SECONDS,
    extra_seconds: int = EXTRA_SECONDS,
    start_on_scheduled_day: bool = False,
    today: date | None = None,
    scores=None,
) -> None:
    """Reject match state changes that do not follow the competition rules."""
    requested_status = update_data.get("status", match.status)
    current_status = match.status
    category = match.category

    if update_data.get("is_ready") is not None:
        if current_status != MatchStatus.Scheduled.value or requested_status != MatchStatus.Scheduled.value:
            raise HTTPException(
                status_code=400,
                detail="Only a scheduled match can be marked or cleared as ready.",
            )
        if update_data["is_ready"]:
            effective_team1 = update_data.get("team1", match.team1)
            effective_team2 = update_data.get("team2", match.team2)
            if not effective_team1 or not effective_team2:
                raise HTTPException(
                    status_code=400,
                    detail="Both teams must be assigned before marking a match ready.",
                )

    if update_data.get("is_concluded") is not None:
        if current_status != MatchStatus.Played.value or requested_status != MatchStatus.Played.value:
            raise HTTPException(
                status_code=400,
                detail="Only a finished match can be marked or reopened as complete.",
            )
        # Marking complete only moves the stream on; the recorded result is not re-judged.
        if not (set(update_data) - {"is_concluded"}):
            return

    if current_status == MatchStatus.Played.value:
        if requested_status != MatchStatus.Played.value:
            raise HTTPException(status_code=400, detail="A finished match cannot move back to an earlier status.")
        _validate_completion(match, {
            "outcome": update_data.get("outcome", match.outcome),
            "duration": update_data.get("duration", match.duration),
            "extra": update_data.get("extra", match.extra),
            "golden_strike": update_data.get("golden_strike", match.golden_strike),
        }, colour_cleared=colour_cleared, regulation_seconds=regulation_seconds, extra_seconds=extra_seconds, scores=scores)
        return

    if current_status == MatchStatus.Scheduled.value:
        premature_fields = {"duration", "extra", "golden_strike", "outcome", "net_points"} & update_data.keys()
        if premature_fields:
            raise HTTPException(
                status_code=400,
                detail="A result or clock value cannot be recorded until the match is in progress.",
            )

    if requested_status < current_status:
        raise HTTPException(status_code=400, detail="A match cannot move back to an earlier status.")

    if current_status == MatchStatus.Scheduled.value and requested_status == MatchStatus.InProgress.value:
        if not match.team1 or not match.team2:
            raise HTTPException(status_code=400, detail="Both teams must be assigned before starting a match.")
        if start_on_scheduled_day and _calendar_day(getattr(match, "scheduled_date", None)) != (today or league_today()):
            raise HTTPException(
                status_code=400,
                detail="This match can be started only on its scheduled date.",
            )

    if requested_status != MatchStatus.Played.value:
        _validate_in_progress_update(match, update_data, extra_seconds=extra_seconds, scores=scores)
        return

    if current_status != MatchStatus.InProgress.value:
        raise HTTPException(
            status_code=400,
            detail="A match must be started before it can be completed.",
        )

    _validate_completion(
        match,
        update_data,
        colour_cleared=colour_cleared,
        regulation_seconds=regulation_seconds,
        extra_seconds=extra_seconds,
        scores=scores,
    )


def _regulation_satisfied(match, duration, colour_cleared: bool, regulation_seconds: int) -> bool:
    if duration == regulation_seconds:
        return True
    # A finished match keeps the duration it was saved with, including an early finish.
    if match.status == MatchStatus.Played.value and duration is not None and duration == match.duration:
        return True
    return bool(colour_cleared) and isinstance(duration, int) and 0 <= duration <= regulation_seconds


def _extra_satisfied(match, extra, colour_cleared: bool, extra_seconds: int) -> bool:
    if extra in (None, 0, extra_seconds):
        return True
    if match.status == MatchStatus.Played.value and extra is not None and extra == match.extra:
        return True
    return bool(colour_cleared) and isinstance(extra, int) and 0 <= extra <= extra_seconds


def _validate_in_progress_update(
    match,
    update_data: dict,
    extra_seconds: int = EXTRA_SECONDS,
    scores=None,
) -> None:
    premature_result_fields = {"duration", "outcome", "net_points"} & update_data.keys()
    if premature_result_fields:
        raise HTTPException(
            status_code=400,
            detail="A result or clock value can be saved only when the match is completed.",
        )

    if update_data.get("golden_strike"):
        _validate_golden_strike_choice(match, scores)

    if "extra" not in update_data:
        return

    extra = update_data["extra"]
    if not is_knockout(match.category):
        raise HTTPException(status_code=400, detail="League matches do not have extra time.")
    if extra not in (0, None):
        raise HTTPException(
            status_code=400,
            detail=f"Extra time starts at 00:00 and is limited to {extra_seconds // 60} minutes.",
        )
    if extra == 0 and getattr(match, "extra_started_at", None) is None and scores is not None:
        if max(scores) >= COINS_PER_COLOR:
            raise HTTPException(
                status_code=400,
                detail="A team has already cleared its 9 coins, so there is no extra time to play.",
            )
        if scores[0] != scores[1]:
            raise HTTPException(
                status_code=400,
                detail="Extra time is played only when the scorecard is level after regulation.",
            )


def _validate_golden_strike_choice(match, scores) -> None:
    """The scorer chose a Golden Strike while the match is live: extra time was played and the scorecard is level."""
    if not is_knockout(match.category):
        raise HTTPException(status_code=400, detail="Only a knockout match can go to a Golden Strike.")
    if getattr(match, "extra_started_at", None) is None:
        raise HTTPException(
            status_code=400,
            detail="A Golden Strike is available only after extra time has been played.",
        )
    if scores is not None and (scores[0] != scores[1] or max(scores) >= COINS_PER_COLOR):
        raise HTTPException(
            status_code=400,
            detail="A Golden Strike is played only when the scorecard is level after extra time.",
        )


def _validate_scorecard_result(update_data: dict, scores) -> None:
    """The recorded result has to be the one the scorecard gives."""
    outcome = update_data.get("outcome")
    golden_strike = bool(update_data.get("golden_strike", False))

    if both_colours_cleared(scores):
        raise HTTPException(
            status_code=400,
            detail="Both teams cannot have all 9 coins off the board. The first colour to clear ends the match.",
        )

    if scores[0] == scores[1]:
        if not golden_strike and outcome != MatchOutcome.NotDecided.value:
            raise HTTPException(
                status_code=400,
                detail="The scorecard is level. Play a Golden Strike for a winner, or record the match as a tie.",
            )
        return

    leader = MatchOutcome.Team1Win.value if scores[0] > scores[1] else MatchOutcome.Team2Win.value
    if golden_strike:
        raise HTTPException(
            status_code=400,
            detail="A Golden Strike is played only when the scorecard is level.",
        )
    if outcome != leader:
        raise HTTPException(
            status_code=400,
            detail="The result must go to the team that leads the scorecard.",
        )


def _validate_completion(
    match,
    update_data: dict,
    colour_cleared: bool = False,
    regulation_seconds: int = REGULATION_SECONDS,
    extra_seconds: int = EXTRA_SECONDS,
    scores=None,
) -> None:
    outcome = update_data.get("outcome")
    duration = update_data.get("duration")
    extra = update_data.get("extra")
    golden_strike = update_data.get("golden_strike", False)

    if match.category == MatchCategory.GoldenStrike.value:
        if outcome not in (MatchOutcome.Team1Win.value, MatchOutcome.Team2Win.value):
            raise HTTPException(status_code=400, detail="Golden Strike must select a winning team.")
        if duration not in (None, 0) or extra not in (None, 0):
            raise HTTPException(status_code=400, detail="Golden Strike does not use regulation or extra time.")
        return

    if not _regulation_satisfied(match, duration, colour_cleared, regulation_seconds):
        raise HTTPException(
            status_code=400,
            detail=f"A match can be completed only after the full {regulation_seconds // 60}-minute regulation period.",
        )

    if match.category == MatchCategory.League.value:
        if extra not in (None, 0) or golden_strike:
            raise HTTPException(
                status_code=400,
                detail=f"League matches have only {regulation_seconds // 60} minutes of regulation time.",
            )
        return

    if not is_knockout(match.category):
        return

    if not _extra_satisfied(match, extra, colour_cleared, extra_seconds):
        raise HTTPException(
            status_code=400,
            detail=f"Extra time is either skipped or played for the full {extra_seconds // 60} minutes.",
        )
    if golden_strike and (
        extra != extra_seconds
        or outcome not in (MatchOutcome.Team1Win.value, MatchOutcome.Team2Win.value)
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Golden Strike is available after the full {extra_seconds // 60} minutes of extra time and must have a winner.",
        )
    if scores is not None:
        _validate_scorecard_result(update_data, scores)

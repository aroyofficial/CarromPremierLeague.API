from enums.match_category import MatchCategory
from enums.match_outcome import MatchOutcome
from enums.match_status import MatchStatus
from fastapi import HTTPException


REGULATION_SECONDS = 15 * 60
EXTRA_SECONDS = 5 * 60

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


def validate_lifecycle_update(match, update_data: dict, colour_cleared: bool = False) -> None:
    """Reject match state changes that do not follow the competition rules."""
    requested_status = update_data.get("status", match.status)
    current_status = match.status
    category = match.category

    if current_status == MatchStatus.Played.value:
        if requested_status != MatchStatus.Played.value:
            raise HTTPException(status_code=400, detail="A finished match cannot move back to an earlier status.")
        _validate_completion(match, {
            "outcome": update_data.get("outcome", match.outcome),
            "duration": update_data.get("duration", match.duration),
            "extra": update_data.get("extra", match.extra),
            "golden_strike": update_data.get("golden_strike", match.golden_strike),
        }, colour_cleared=colour_cleared)
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

    if requested_status != MatchStatus.Played.value:
        _validate_in_progress_update(match, update_data)
        return

    if current_status != MatchStatus.InProgress.value:
        raise HTTPException(
            status_code=400,
            detail="A match must be started before it can be completed.",
        )

    _validate_completion(match, update_data, colour_cleared=colour_cleared)


def _regulation_satisfied(match, duration, colour_cleared: bool) -> bool:
    if duration == REGULATION_SECONDS:
        return True
    # A finished match keeps the duration it was saved with, including an early finish.
    if match.status == MatchStatus.Played.value and duration is not None and duration == match.duration:
        return True
    return bool(colour_cleared) and isinstance(duration, int) and 0 <= duration <= REGULATION_SECONDS


def _extra_satisfied(match, extra, colour_cleared: bool) -> bool:
    if extra in (None, 0, EXTRA_SECONDS):
        return True
    if match.status == MatchStatus.Played.value and extra is not None and extra == match.extra:
        return True
    return bool(colour_cleared) and isinstance(extra, int) and 0 <= extra <= EXTRA_SECONDS


def _validate_in_progress_update(match, update_data: dict) -> None:
    premature_result_fields = {"duration", "golden_strike", "outcome", "net_points"} & update_data.keys()
    if premature_result_fields:
        raise HTTPException(
            status_code=400,
            detail="A result or clock value can be saved only when the match is completed.",
        )

    if "extra" not in update_data:
        return

    extra = update_data["extra"]
    if not is_knockout(match.category):
        raise HTTPException(status_code=400, detail="League matches do not have extra time.")
    if extra not in (0, None):
        raise HTTPException(status_code=400, detail="Extra time starts at 00:00 and is limited to five minutes.")


def _validate_completion(match, update_data: dict, colour_cleared: bool = False) -> None:
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

    if not _regulation_satisfied(match, duration, colour_cleared):
        raise HTTPException(
            status_code=400,
            detail="A match can be completed only after the full 15-minute regulation period.",
        )

    if match.category == MatchCategory.League.value:
        if extra not in (None, 0) or golden_strike:
            raise HTTPException(status_code=400, detail="League matches have only 15 minutes of regulation time.")
        return

    if not is_knockout(match.category):
        return

    if not _extra_satisfied(match, extra, colour_cleared):
        raise HTTPException(
            status_code=400,
            detail="Extra time is either skipped or played for the full five minutes.",
        )
    if golden_strike and (
        extra != EXTRA_SECONDS
        or outcome not in (MatchOutcome.Team1Win.value, MatchOutcome.Team2Win.value)
    ):
        raise HTTPException(
            status_code=400,
            detail="Golden Strike is available after the full five minutes of extra time and must have a winner.",
        )

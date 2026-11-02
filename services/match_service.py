from typing import List
from schemas.match_schema import (
    MatchCreateRequest,
    MatchUpdateRequest,
    MatchResponse,
    MatchOrderResponse,
    MatchStatsUpsertRequest,
    MatchStatResponse
)
from repositories.match_repository import MatchRepository
from fastapi import HTTPException
from enums.match_status import MatchStatus
from enums.match_category import MatchCategory
from enums.match_outcome import MatchOutcome
from services.match_rules import validate_lifecycle_update

# One team plays the white coins and the other plays the black coins.
COINS_PER_COLOR = 9


class MatchService:

    def __init__(self, repository: MatchRepository):
        self.repository = repository

    def create(self, request: MatchCreateRequest) -> MatchResponse:
        if request.status != MatchStatus.Scheduled:
            raise HTTPException(status_code=400, detail="New matches must be scheduled before they can start.")
        if request.category == MatchCategory.GoldenStrike:
            raise HTTPException(status_code=400, detail="Golden Strike is created automatically after a tied knockout match.")
        if request.duration is not None or request.extra is not None or request.golden_strike:
            raise HTTPException(
                status_code=400,
                detail="Match timing and Golden Strike are set by the match lifecycle, not when scheduling.",
            )
        if request.outcome != MatchOutcome.NotDecided or request.net_points is not None:
            raise HTTPException(status_code=400, detail="A newly scheduled match cannot include a result.")
        return self.repository.create(request)

    def get_by_id(self, match_id: int) -> MatchResponse:
        match = self.repository.get_by_id(match_id)
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")
        return match

    def get_all(self, season_id: int = None) -> List[MatchResponse]:
        return self.repository.get_all(season_id=season_id)

    def update(self, match_id: int, request: MatchUpdateRequest) -> MatchResponse:
        match = self.get_by_id(match_id)
        validate_lifecycle_update(match, request.model_dump(exclude_unset=True))
        match = self.repository.update(match_id, request)
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")
        return match

    def reset(self, match_id: int) -> MatchResponse:
        self.get_by_id(match_id)
        reset = self.repository.reset(match_id)
        if not reset:
            raise HTTPException(status_code=404, detail="Match not found")
        return reset

    def delete(self, match_id: int) -> bool:
        if not self.repository.soft_delete(match_id):
            raise HTTPException(status_code=404, detail="Match not found")
        return True
    
    def get_next_match_order(self, season_id: int) -> MatchOrderResponse:
        return self.repository.get_order(season_id)

    def get_stats(self, match_id: int) -> List[MatchStatResponse]:
        self.get_by_id(match_id)
        return self.repository.get_stats(match_id)

    def upsert_stats(self, match_id: int, request: MatchStatsUpsertRequest) -> List[MatchStatResponse]:
        match = self.repository.get_by_id(match_id)
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")

        player_ids = [item.player_id for item in request.stats]
        if len(player_ids) != len(set(player_ids)):
            raise HTTPException(status_code=400, detail="Duplicate player_id in stats payload")

        for item in request.stats:
            derived_fines = item.single_fines + 2 * item.double_fines
            if derived_fines > 255:
                raise HTTPException(status_code=400, detail="Fines for one player cannot exceed 255 coins.")
            item.coins_fined = derived_fines
        self._validate_coin_counts(match_id, request.stats)
        return self.repository.upsert_stats(match_id, request.stats)

    def _validate_coin_counts(self, match_id: int, stats: list) -> None:
        roster = {
            int(row["PlayerId"]): int(row["TeamId"])
            for row in self.repository.players_on_match_teams(match_id)
        }
        for item in stats:
            if item.player_id not in roster:
                raise HTTPException(
                    status_code=400,
                    detail="A scorecard player must belong to one of the two teams in the match.",
                )

        pocketed: dict[int, int] = {}
        opponent: dict[int, int] = {}
        fined: dict[int, int] = {}
        for row in self.repository.player_coin_lines(match_id):
            player_id = int(row["PlayerId"])
            team_id = int(row["TeamId"])
            pocketed[player_id] = int(row["CoinsPocketed"] or 0)
            opponent[player_id] = int(row["OpponentCoinsPocketed"] or 0)
            fined[player_id] = int(row["SingleFines"] or 0) + 2 * int(row["DoubleFines"] or 0)
            roster.setdefault(player_id, team_id)

        for item in stats:
            pocketed[item.player_id] = item.coins_pocketed
            opponent[item.player_id] = item.opponent_coins_pocketed
            fined[item.player_id] = item.coins_fined

        own = {}
        taken = {}
        fines = {}
        for player_id, team_id in roster.items():
            if player_id not in pocketed and player_id not in opponent:
                continue
            own[team_id] = own.get(team_id, 0) + pocketed.get(player_id, 0)
            taken[team_id] = taken.get(team_id, 0) + opponent.get(player_id, 0)
            fines[team_id] = fines.get(team_id, 0) + fined.get(player_id, 0)

        # A fine puts one of the team's pocketed coins back on the board, so it can be pocketed again.
        # A fine the team cannot pay yet is due and returns nothing.
        teams = list(own.keys() | taken.keys())
        for team_id in teams:
            other_taken = sum(count for other_id, count in taken.items() if other_id != team_id)
            off_board = max(0, own.get(team_id, 0) - fines.get(team_id, 0)) + other_taken
            if off_board > COINS_PER_COLOR:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Each colour has only 9 coins. Pocketed coins minus fined coins, plus the coins "
                        "the other team pocketed of that colour, cannot go above 9."
                    ),
                )

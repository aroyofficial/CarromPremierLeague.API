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

# One team plays the white coins and the other plays the black coins.
COINS_PER_COLOR = 9


class MatchService:

    def __init__(self, repository: MatchRepository):
        self.repository = repository

    def create(self, request: MatchCreateRequest) -> MatchResponse:
        return self.repository.create(request)

    def get_by_id(self, match_id: int) -> MatchResponse:
        match = self.repository.get_by_id(match_id)
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")
        return match

    def get_all(self, season_id: int = None) -> List[MatchResponse]:
        return self.repository.get_all(season_id=season_id)

    def update(self, match_id: int, request: MatchUpdateRequest) -> MatchResponse:
        match = self.repository.update(match_id, request)
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")
        return match

    def delete(self, match_id: int) -> bool:
        if not self.repository.soft_delete(match_id):
            raise HTTPException(status_code=404, detail="Match not found")
        return True
    
    def get_next_match_order(self, season_id: int) -> MatchOrderResponse:
        return self.repository.get_order(season_id)

    def upsert_stats(self, match_id: int, request: MatchStatsUpsertRequest) -> List[MatchStatResponse]:
        match = self.repository.get_by_id(match_id)
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")

        player_ids = [item.player_id for item in request.stats]
        if len(player_ids) != len(set(player_ids)):
            raise HTTPException(status_code=400, detail="Duplicate player_id in stats payload")

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
        for row in self.repository.player_coin_lines(match_id):
            player_id = int(row["PlayerId"])
            team_id = int(row["TeamId"])
            pocketed[player_id] = int(row["CoinsPocketed"] or 0)
            opponent[player_id] = int(row["OpponentCoinsPocketed"] or 0)
            roster.setdefault(player_id, team_id)

        for item in stats:
            pocketed[item.player_id] = item.coins_pocketed

        own = {}
        taken = {}
        for player_id, team_id in roster.items():
            if player_id not in pocketed and player_id not in opponent:
                continue
            own[team_id] = own.get(team_id, 0) + pocketed.get(player_id, 0)
            taken[team_id] = taken.get(team_id, 0) + opponent.get(player_id, 0)

        teams = list(own.keys() | taken.keys())
        for team_id in teams:
            other_taken = sum(count for other_id, count in taken.items() if other_id != team_id)
            if own.get(team_id, 0) + other_taken > COINS_PER_COLOR:
                raise HTTPException(
                    status_code=400,
                    detail="Each colour has only 9 coins. White and black coin counts cannot go above 9.",
                )

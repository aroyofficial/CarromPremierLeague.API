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

        return self.repository.upsert_stats(match_id, request.stats)

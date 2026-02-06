from typing import List
from schemas.match_schema import (
    MatchCreateRequest,
    MatchUpdateRequest,
    MatchResponse,
    MatchOrderResponse
)
from repositories.match_repository import MatchRepository
from fastapi import HTTPException


class MatchService:

    def __init__(self, repository: MatchRepository):
        self.repository = repository

    def create(self, request: MatchCreateRequest) -> MatchResponse:
        if request.team1 == request.team2:
            raise HTTPException(status_code=400, detail="Team1 and Team2 cannot be same")

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
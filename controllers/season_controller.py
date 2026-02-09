from typing import List
from fastapi import HTTPException
from repositories.season_repository import SeasonRepository
from services.season_service import SeasonService
from schemas.season_schema import (
    SeasonCreateRequest,
    SeasonUpdateRequest,
    SeasonResponse,
    LeagueTableResponse
)


class SeasonController:

    def __init__(self, db):
        self.repository = SeasonRepository(db)
        self.service = SeasonService(self.repository)

    def create(self, request: SeasonCreateRequest) -> SeasonResponse:
        return self.service.create_season(request)

    def get_by_id(self, season_id: int) -> SeasonResponse:
        return self.service.get_season(season_id)

    def get_all(self):
        return self.service.get_all_seasons()

    def update(self, season_id: int, request: SeasonUpdateRequest) -> SeasonResponse:
        return self.service.update_season(season_id, request)

    def delete(self, season_id: int) -> dict:
        return self.service.delete_season(season_id)
    
    def get_league_table(self, season_id: int) -> LeagueTableResponse:
        return self.service.get_league_table(season_id)
from typing import List
from fastapi import HTTPException
from repositories.match_repository import MatchRepository
from repositories.schedule_repository import ScheduleRepository
from repositories.season_repository import SeasonRepository
from services.season_service import SeasonService
from schemas.schedule_schema import SeasonScheduleResponse
from schemas.season_schema import (
    SeasonCreateRequest,
    SeasonUpdateRequest,
    SeasonResponse,
    LeagueTableResponse
)


class SeasonController:

    def __init__(self, db):
        self.repository = SeasonRepository(db)
        self.schedule_repository = ScheduleRepository(db)
        self.match_repository = MatchRepository(db)
        self.service = SeasonService(self.repository)

    def create(self, request: SeasonCreateRequest) -> SeasonResponse:
        return self.service.create_season(request)

    def get_by_id(self, season_id: int) -> SeasonResponse:
        return self.service.get_season(season_id)

    def get_all(self):
        return self.service.get_all_seasons()

    def update(self, season_id: int, request: SeasonUpdateRequest) -> SeasonResponse:
        return self.service.update_season(season_id, request)

    def delete(self, season_id: int) -> bool:
        self.service.delete_season(season_id)
        return True
    
    def get_teams(self, season_id: int):
        return self.service.get_teams(season_id)

    def add_team(self, season_id: int, team_id: int):
        return self.service.add_team(season_id, team_id)

    def remove_team(self, season_id: int, team_id: int) -> bool:
        return self.service.remove_team(season_id, team_id)

    def schedule_matches(self, season_id: int, rounds: int = 1):
        return self.service.schedule_matches(season_id, self.match_repository, rounds=rounds)

    def get_league_table(self, season_id: int) -> LeagueTableResponse:
        return self.service.get_league_table(season_id)

    def get_schedule(self, season_id: int) -> SeasonScheduleResponse:
        self.service.get_season(season_id)
        return self.schedule_repository.get_schedule(season_id)
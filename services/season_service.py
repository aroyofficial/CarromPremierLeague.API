from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import HTTPException

from schemas.season_schema import (
    SeasonCreateRequest,
    SeasonUpdateRequest,
    SeasonResponse,
    LeagueTableResponse,
    SeasonScheduleBuildResponse,
    SeasonTeamResponse,
)
from repositories.season_repository import SeasonRepository
from services.league_schedule import DEFAULT_ROUNDS, plan_league_schedule


class SeasonService:

    def __init__(self, repository: SeasonRepository):
        self.repository = repository

    def create_season(self, request: SeasonCreateRequest) -> SeasonResponse:
        if self.repository.exists_by_name(request.name):
            raise HTTPException(status_code=400, detail="Season already exists")

        if request.start_date and request.end_date:
            if request.end_date < request.start_date:
                raise HTTPException(status_code=400, detail="End date cannot be before start date")

        return self.repository.create(request)

    def get_season(self, season_id: int) -> SeasonResponse:
        season = self.repository.get_by_id(season_id)

        if not season:
            raise HTTPException(status_code=404, detail="Season not found")

        return season

    def get_all_seasons(self) -> List[SeasonResponse]:
        return self.repository.get_all()

    def update_season(self, season_id: int, request: SeasonUpdateRequest) -> SeasonResponse:
        existing = self.repository.get_by_id(season_id)

        if not existing:
            raise HTTPException(status_code=404, detail="Season not found")

        update_data = request.model_dump(exclude_unset=True)

        if "start_date" in update_data and "end_date" in update_data:
            if update_data["end_date"] and update_data["start_date"]:
                if update_data["end_date"] < update_data["start_date"]:
                    raise HTTPException(status_code=400, detail="End date cannot be before start date")

        updated = self.repository.update(season_id, request)

        if not updated:
            raise HTTPException(status_code=404, detail="Season not found")

        return updated

    def delete_season(self, season_id: int) -> dict:
        existing = self.repository.get_by_id(season_id)

        if not existing:
            raise HTTPException(status_code=404, detail="Season not found")

        deleted = self.repository.soft_delete(season_id)

        if not deleted:
            raise HTTPException(status_code=400, detail="Failed to delete season")

        return {"message": "Season deleted successfully"}
    
    def get_teams(self, season_id: int) -> List[SeasonTeamResponse]:
        self.get_season(season_id)
        return self.repository.get_teams(season_id)

    def add_team(self, season_id: int, team_id: int) -> List[SeasonTeamResponse]:
        self.get_season(season_id)
        if not self.repository.team_exists(team_id):
            raise HTTPException(status_code=404, detail="Team not found")
        self.repository.add_team(season_id, team_id)
        return self.repository.get_teams(season_id)

    def remove_team(self, season_id: int, team_id: int) -> bool:
        self.get_season(season_id)
        if not self.repository.remove_team(season_id, team_id):
            raise HTTPException(status_code=404, detail="Team is not part of this season")
        return True

    def schedule_matches(
        self, season_id: int, match_repository, today=None, rounds: int = DEFAULT_ROUNDS
    ) -> SeasonScheduleBuildResponse:
        season = self.get_season(season_id)
        if today is None:
            today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
        teams = [team.team_id for team in self.get_teams(season_id)]
        try:
            pairs = plan_league_schedule(teams, match_repository.get_all(season_id), season.start_date, today, rounds)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        removed, created = match_repository.replace_unplayed_matches(season_id, pairs, season.start_date)
        return SeasonScheduleBuildResponse(removed=removed, created=created, rounds=rounds)

    def get_league_table(self, season_id: int) -> LeagueTableResponse:
        if season_id <= 0:
            raise ValueError("Invalid season id")

        season = self.repository.get_by_id(season_id)
        if not season:
            raise ValueError("Season not found")

        return self.repository.get_league_table(season_id)
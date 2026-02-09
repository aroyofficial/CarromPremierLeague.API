from typing import List

from fastapi import HTTPException

from schemas.season_schema import (
    SeasonCreateRequest,
    SeasonUpdateRequest,
    SeasonResponse,
    LeagueTableResponse
)
from repositories.season_repository import SeasonRepository


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
    
    def get_league_table(self, season_id: int) -> LeagueTableResponse:
        if season_id <= 0:
            raise ValueError("Invalid season id")

        season = self.repository.get_by_id(season_id)
        if not season:
            raise ValueError("Season not found")

        return self.repository.get_league_table(season_id)
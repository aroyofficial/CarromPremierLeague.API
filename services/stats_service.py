from fastapi import HTTPException
from repositories.stats_repository import StatsRepository


class StatsService:

    def __init__(self, repository: StatsRepository):
        self.repository = repository

    def get_head_to_head(self, team1_id: int, team2_id: int):
        if team1_id == team2_id:
            raise HTTPException(
                status_code=400,
                detail="Cannot compare a team with itself"
            )

        return self.repository.get_head_to_head(team1_id, team2_id)

    def get_season_top_coin_potters(self, season_id: int, limit: int = 3):
        if season_id <= 0:
            raise HTTPException(
                status_code=400,
                detail="Invalid season id"
            )

        if limit <= 0:
            raise HTTPException(
                status_code=400,
                detail="Limit must be greater than zero"
            )

        return self.repository.get_season_top_coin_potters(season_id, limit)

    def has_completed_league_matches(self, season_id: int):
        if season_id <= 0:
            raise HTTPException(
                status_code=400,
                detail="Invalid season id"
            )
        return self.repository.has_completed_league_matches(season_id)

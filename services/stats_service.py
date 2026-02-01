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

from fastapi import HTTPException
from typing import List
from services.team_service import TeamService
from schemas.team_schema import (
    TeamCreateRequest,
    TeamUpdateRequest,
    TeamResponse
)


class TeamController:

    def __init__(self, service: TeamService):
        self.service = service

    def get_all(self) -> List[TeamResponse]:
        return self.service.get_all()

    def get_by_id(self, team_id: int) -> TeamResponse:
        team = self.service.get_by_id(team_id)

        if not team:
            raise HTTPException(
                status_code=404,
                detail="Team not found"
            )

        return team

    def create(self, request: TeamCreateRequest) -> TeamResponse:
        return self.service.create(request)

    def update(self, team_id: int, request: TeamUpdateRequest) -> TeamResponse:
        team = self.service.update(team_id, request)

        if not team:
            raise HTTPException(
                status_code=404,
                detail="Team not found"
            )

        return team

    def delete(self, team_id: int) -> dict:
        success = self.service.delete(team_id)

        if not success:
            raise HTTPException(
                status_code=404,
                detail="Team not found"
            )

        return {"message": "Team deleted successfully"}

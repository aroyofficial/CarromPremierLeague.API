from typing import List, Optional
from repositories.team_repository import TeamRepository
from schemas.team_schema import (
    TeamCreateRequest,
    TeamUpdateRequest,
    TeamResponse
)


class TeamService:

    def __init__(self, repository: TeamRepository):
        self.repository = repository

    def get_all(self) -> List[TeamResponse]:
        return self.repository.get_all()

    def get_by_id(self, team_id: int) -> Optional[TeamResponse]:
        return self.repository.get_by_id(team_id)

    def create(self, request: TeamCreateRequest) -> TeamResponse:
        return self.repository.create(request)

    def update(self, team_id: int, request: TeamUpdateRequest) -> Optional[TeamResponse]:
        return self.repository.update(team_id, request)

    def delete(self, team_id: int) -> bool:
        return self.repository.delete(team_id)

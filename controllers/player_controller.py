from typing import List
from services.player_service import PlayerService
from schemas.player_schema import (
    PlayerCreateRequest,
    PlayerUpdateRequest,
    PlayerResponse
)


class PlayerController:

    def __init__(self, service: PlayerService):
        self.service = service

    def get_all(self) -> List[PlayerResponse]:
        return self.service.get_all()

    def get_by_id(self, player_id: int) -> PlayerResponse:
        return self.service.get_by_id(player_id)

    def create(self, request: PlayerCreateRequest) -> PlayerResponse:
        return self.service.create(request)

    def update(self, player_id: int, request: PlayerUpdateRequest) -> PlayerResponse:
        return self.service.update(player_id, request)

    def delete(self, player_id: int) -> dict:
        self.service.delete(player_id)
        return {"message": "Player deleted successfully"}
from services.match_service import MatchService
from schemas.match_schema import (
    MatchCreateRequest,
    MatchUpdateRequest,
    MatchStatsUpsertRequest
)


class MatchController:

    def __init__(self, service: MatchService):
        self.service = service

    def create(self, request: MatchCreateRequest):
        return self.service.create(request)

    def get_by_id(self, match_id: int):
        return self.service.get_by_id(match_id)

    def get_all(self, season_id: int = None):
        return self.service.get_all(season_id=season_id)

    def update(self, match_id: int, request: MatchUpdateRequest):
        return self.service.update(match_id, request)

    def delete(self, match_id: int):
        return self.service.delete(match_id)
    
    def get_next_match_order(self, season_id: int):
        return self.service.get_next_match_order(season_id)

    def upsert_stats(self, match_id: int, request: MatchStatsUpsertRequest):
        return self.service.upsert_stats(match_id, request)

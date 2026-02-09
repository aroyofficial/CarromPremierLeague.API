from services.stats_service import StatsService


class StatsController:

    def __init__(self, service: StatsService):
        self.service = service

    def get_head_to_head(self, team1_id: int, team2_id: int):
        return self.service.get_head_to_head(team1_id, team2_id)

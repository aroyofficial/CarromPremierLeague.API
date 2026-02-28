from services.stats_service import StatsService


class StatsController:

    def __init__(self, service: StatsService):
        self.service = service

    def get_head_to_head(self, team1_id: int, team2_id: int):
        return self.service.get_head_to_head(team1_id, team2_id)

    def get_season_top_coin_potters(self, season_id: int, limit: int = 3):
        return self.service.get_season_top_coin_potters(season_id, limit)

    def has_completed_league_matches(self, season_id: int):
        return self.service.has_completed_league_matches(season_id)

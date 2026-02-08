from services.roster_service import RosterService


class RosterController:

    def __init__(self, service: RosterService):
        self.service = service

    def assign_player(self, season_id: int, team_id: int, player_id: int):
        return self.service.assign_player(
            season_id,
            team_id,
            player_id
        )
    
    def remove_player(self, season_id: int, player_id: int):
        return self.service.remove_player(season_id, player_id)

    def get_team_roster(self, season_id: int, team_id: int):
        return self.service.get_team_roster(season_id, team_id)

    def get_player_history(self, player_id: int):
        return self.service.get_player_history(player_id)

    def get_season_rosters(self, season_id: int):
        return self.service.get_season_rosters(season_id)

    def get_season_team_players_history(self, season_id: int):
        return self.service.get_season_team_players_history(season_id)
from fastapi import HTTPException
import mysql.connector

from repositories.roster_repository import RosterRepository


class RosterService:

    def __init__(self, repository: RosterRepository):
        self.repository = repository

    def assign_player(self, season_id: int, team_id: int, player_id: int):

        try:
            result = self.repository.assign_player(
                season_id,
                team_id,
                player_id
            )

            if not result:
                raise HTTPException(
                    status_code=500,
                    detail="Assignment failed"
                )

            return result

        except mysql.connector.Error as e:
            raise HTTPException(
                status_code=400,
                detail=str(e.msg)
            )
        
    def remove_player(self, season_id: int, player_id: int):
        removed = self.repository.remove_player(season_id, player_id)

        if not removed:
            raise HTTPException(status_code=404, detail="Roster entry not found")

        return True

    def get_team_roster(self, season_id: int, team_id: int):
        return self.repository.get_team_roster(season_id, team_id)

    def get_player_history(self, player_id: int):
        return self.repository.get_player_history(player_id)

    def get_season_rosters(self, season_id: int):
        return self.repository.get_season_rosters(season_id)

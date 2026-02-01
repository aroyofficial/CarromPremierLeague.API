from typing import Optional

from schemas.roster_schema import (
    RosterResponse,
    TeamRosterResponse,
    TeamRosterPlayer,
    PlayerSeasonHistoryResponse,
    PlayerSeasonHistoryItem,
    SeasonRosterResponse,
    SeasonRosterItem
)


class RosterRepository:

    def __init__(self, db):
        self.db = db

    def assign_player(self, season_id: int, team_id: int, player_id: int) -> RosterResponse:
        cursor = self.db.cursor()

        cursor.callproc(
            "usp_AssignPlayerToTeam",
            [season_id, team_id, player_id]
        )

        self.db.commit()

        return self.get_by_player_season(player_id, season_id)

    def get_by_player_season(self, player_id: int, season_id: int) -> Optional[RosterResponse]:
        query = """
            SELECT Id, PlayerId, SeasonId, TeamId
            FROM tblPlayersSeasonsTeams
            WHERE PlayerId = %s
              AND SeasonId = %s
              AND Void = 0
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (player_id, season_id))
        row = cursor.fetchone()

        if not row:
            return None

        return RosterResponse(
            id=row["Id"],
            player_id=row["PlayerId"],
            season_id=row["SeasonId"],
            team_id=row["TeamId"]
        )
    
    def remove_player(self, season_id: int, player_id: int) -> bool:
        query = """
            UPDATE tblPlayersSeasonsTeams
            SET Void = 1
            WHERE SeasonId = %s
              AND PlayerId = %s
              AND Void = 0
        """

        cursor = self.db.cursor()
        cursor.execute(query, (season_id, player_id))
        self.db.commit()

        return cursor.rowcount > 0

    def get_team_roster(self, season_id: int, team_id: int) -> TeamRosterResponse:
        query = """
            SELECT p.Id AS PlayerId,
                   p.FirstName,
                   p.LastName,
                   p.AvatarUrl
            FROM tblPlayersSeasonsTeams pst
            JOIN tblPlayers p ON pst.PlayerId = p.Id
            WHERE pst.SeasonId = %s
              AND pst.TeamId = %s
              AND pst.Void = 0
              AND p.Void = 0
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (season_id, team_id))
        rows = cursor.fetchall()

        players = [
            TeamRosterPlayer(
                player_id=row["PlayerId"],
                first_name=row["FirstName"],
                last_name=row["LastName"],
                avatar_url=row["AvatarUrl"]
            )
            for row in rows
        ]

        return TeamRosterResponse(
            team_id=team_id,
            season_id=season_id,
            players=players
        )

    def get_player_history(self, player_id: int) -> PlayerSeasonHistoryResponse:
        query = """
            SELECT s.Id AS SeasonId,
                   s.Name AS SeasonName,
                   t.Id AS TeamId,
                   t.Name AS TeamName
            FROM tblPlayersSeasonsTeams pst
            JOIN tblSeasons s ON pst.SeasonId = s.Id
            JOIN tblTeams t ON pst.TeamId = t.Id
            WHERE pst.PlayerId = %s
              AND pst.Void = 0
            ORDER BY s.Id DESC
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (player_id,))
        rows = cursor.fetchall()

        seasons = [
            PlayerSeasonHistoryItem(
                season_id=row["SeasonId"],
                season_name=row["SeasonName"],
                team_id=row["TeamId"],
                team_name=row["TeamName"]
            )
            for row in rows
        ]

        return PlayerSeasonHistoryResponse(
            player_id=player_id,
            seasons=seasons
        )

    def get_season_rosters(self, season_id: int) -> SeasonRosterResponse:
        query = """
            SELECT t.Id AS TeamId,
                   t.Name AS TeamName,
                   p.Id AS PlayerId,
                   p.FirstName,
                   p.LastName
            FROM tblPlayersSeasonsTeams pst
            JOIN tblTeams t ON pst.TeamId = t.Id
            JOIN tblPlayers p ON pst.PlayerId = p.Id
            WHERE pst.SeasonId = %s
              AND pst.Void = 0
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (season_id,))
        rows = cursor.fetchall()

        rosters = [
            SeasonRosterItem(
                team_id=row["TeamId"],
                team_name=row["TeamName"],
                player_id=row["PlayerId"],
                first_name=row["FirstName"],
                last_name=row["LastName"]
            )
            for row in rows
        ]

        return SeasonRosterResponse(
            season_id=season_id,
            rosters=rosters
        )

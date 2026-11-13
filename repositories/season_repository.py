from datetime import datetime, timedelta, timezone
from typing import Optional, List

from schemas.season_schema import (
    SeasonCreateRequest,
    SeasonUpdateRequest,
    SeasonResponse,
    LeagueTableResponse,
    LeagueTableStanding,
    SeasonTeamResponse,
)


def arrange_standings(standings: list[LeagueTableStanding]) -> list[LeagueTableStanding]:
    """Keep every team. With no completed match, list them A to Z."""
    if standings and all(row.matches_played == 0 for row in standings):
        return sorted(standings, key=lambda row: row.team_name.casefold())
    return standings


def show_placeholder_table(start_date, today, played_match_count: int) -> bool:
    """A season that has not reached its start date shows a blank table.

    Completed matches still count. A future start date must not hide results
    that have already been saved.
    """
    if start_date is None or start_date <= today:
        return False
    return int(played_match_count or 0) == 0


class SeasonRepository:

    def __init__(self, db):
        self.db = db

    @staticmethod
    def _map_row_to_schema(row) -> Optional[SeasonResponse]:
        if not row:
            return None

        return SeasonResponse(
            id=row["Id"],
            name=row["Name"],
            start_date=row["StartDate"],
            end_date=row["EndDate"],
            show=bool(row.get("Show")),
        )

    def create(self, request: SeasonCreateRequest) -> SeasonResponse:
        query = """
            INSERT INTO tblSeasons (Name, StartDate, EndDate, [Show])
            VALUES (%s, %s, %s, %s)
        """

        cursor = self.db.cursor()
        cursor.execute(
            query,
            (
                request.name,
                request.start_date,
                request.end_date,
                request.show,
            )
        )

        season_id = cursor.lastrowid
        return self.get_by_id(season_id)

    def get_by_id(self, season_id: int) -> Optional[SeasonResponse]:
        query = """
            SELECT Id, Name, StartDate, EndDate, [Show]
            FROM tblSeasons
            WHERE Id = %s AND Void = 0
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (season_id,))
        row = cursor.fetchone()

        return self._map_row_to_schema(row)

    def get_all(self) -> List[SeasonResponse]:
        query = """
            SELECT Id, Name, StartDate, EndDate, [Show]
            FROM tblSeasons
            WHERE Void = 0
            ORDER BY Id DESC
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query)
        rows = cursor.fetchall()

        return [self._map_row_to_schema(row) for row in rows]

    def update(self, season_id: int, request: SeasonUpdateRequest) -> Optional[SeasonResponse]:
        update_data = request.model_dump(exclude_unset=True)

        if not update_data:
            return self.get_by_id(season_id)

        field_mapping = {
            "name": "Name",
            "start_date": "StartDate",
            "end_date": "EndDate",
            "show": "[Show]",
        }

        fields = []
        values = []

        for key, value in update_data.items():
            if key in field_mapping:
                fields.append(f"{field_mapping[key]} = %s")
                values.append(str(value) if key == "logo_url" else value)

        if not fields:
            return self.get_by_id(season_id)

        query = f"""
            UPDATE tblSeasons
            SET {', '.join(fields)}
            WHERE Id = %s AND Void = 0
        """

        values.append(season_id)

        cursor = self.db.cursor()
        cursor.execute(query, tuple(values))
        self.db.commit()

        return self.get_by_id(season_id)

    def soft_delete(self, season_id: int) -> bool:
        query = """
            UPDATE tblSeasons
            SET Void = 1
            WHERE Id = %s
        """

        cursor = self.db.cursor()
        cursor.execute(query, (season_id,))

        return cursor.rowcount > 0

    def get_teams(self, season_id: int) -> List[SeasonTeamResponse]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT t.Id AS TeamId, t.Name AS TeamName, t.LogoUrl
            FROM tblSeasonsTeams st
            INNER JOIN tblTeams t ON t.Id = st.TeamId AND t.Void = 0
            WHERE st.SeasonId = %s AND st.Void = 0
            ORDER BY t.Name
            """,
            (season_id,),
        )
        return [
            SeasonTeamResponse(team_id=row["TeamId"], team_name=row["TeamName"], logo_url=row["LogoUrl"])
            for row in cursor.fetchall()
        ]

    def add_team(self, season_id: int, team_id: int) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblSeasonsTeams
            SET Void = 0, UpdatedAt = SYSUTCDATETIME()
            WHERE SeasonId = %s AND TeamId = %s
            """,
            (season_id, team_id),
        )
        if cursor.rowcount == 0:
            cursor.execute(
                "INSERT INTO tblSeasonsTeams (SeasonId, TeamId) VALUES (%s, %s)",
                (season_id, team_id),
            )
        self.db.commit()

    def remove_team(self, season_id: int, team_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblSeasonsTeams
            SET Void = 1, UpdatedAt = SYSUTCDATETIME()
            WHERE SeasonId = %s AND TeamId = %s AND Void = 0
            """,
            (season_id, team_id),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def team_exists(self, team_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute("SELECT 1 FROM tblTeams WHERE Id = %s AND Void = 0", (team_id,))
        return cursor.fetchone() is not None

    def exists_by_name(self, name: str) -> bool:
        query = """
            SELECT 1
            FROM tblSeasons
            WHERE Name = %s AND Void = 0
            LIMIT 1
        """

        cursor = self.db.cursor()
        cursor.execute(query, (name,))
        return cursor.fetchone() is not None

    def _played_match_count(self, season_id: int) -> int:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT COUNT(*) AS PlayedCount
            FROM tblMatches
            WHERE SeasonId = %s
              AND Status = 3
              AND Void = 0
            """,
            (season_id,),
        )
        row = cursor.fetchone() or {}
        return int(row.get("PlayedCount") or 0)

    def _upcoming_league_table(self, season_id: int) -> LeagueTableResponse:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT t.Id AS TeamId, t.Name AS TeamName
            FROM tblSeasonsTeams st
            INNER JOIN tblTeams t
                ON t.Id = st.TeamId
               AND t.Void = 0
            WHERE st.SeasonId = %s
              AND st.Void = 0
            ORDER BY t.Name
            """,
            (season_id,),
        )
        rows = cursor.fetchall()
        return LeagueTableResponse(
            standings=[
                LeagueTableStanding(
                    team_id=row["TeamId"],
                    team_name=row["TeamName"],
                    matches_played=0,
                    wins=0,
                    draws=0,
                    points=0,
                    net_points=0,
                    head_to_head_wins=0,
                    golden_strike_wins=0,
                )
                for row in rows
            ],
            season_status=1,
            data_available=len(rows) > 0,
        )

    def get_league_table(self, season_id: int) -> LeagueTableResponse:
        season = self.get_by_id(season_id)
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
        if show_placeholder_table(
            season.start_date if season else None,
            today,
            self._played_match_count(season_id),
        ):
            table = self._upcoming_league_table(season_id)
            table.standings = arrange_standings(table.standings)
            return table

        cursor = self.db.cursor(dictionary=True)
        cursor.execute("EXEC dbo.usp_GetLeagueTable ?", (season_id,))

        result = LeagueTableResponse(
            standings=[],
            season_status=1
        )

        for row in cursor.fetchall():
            is_winner = bool(row.get("IsWinner", 0))
            result.standings.append(
                LeagueTableStanding(
                    team_id=row["TeamId"],
                    team_name=row["TeamName"],
                    matches_played=int(row.get("MatchesPlayed") or 0),
                    wins=int(row.get("Wins") or 0),
                    draws=int(row.get("Draws") or 0),
                    points=int(row.get("Points") or 0),
                    net_points=int(row.get("TotalNetPoints") or 0),
                    head_to_head_wins=int(row.get("HeadToHeadWins") or 0),
                    golden_strike_wins=int(row.get("GoldenStrikeWins") or 0),
                )
            )
            if is_winner:
                result.winner_id = int(row["TeamId"])

        while cursor.nextset():
            status_row = cursor.fetchone()
            if status_row and "SeasonStatus" in status_row:
                result.season_status = int(status_row["SeasonStatus"])
                break
        cursor.close()

        count_cursor = self.db.cursor(dictionary=True)
        count_cursor.execute(
            """
            SELECT COUNT(*) AS MatchCount
            FROM tblMatches
            WHERE SeasonId = %s AND Void = 0
            """,
            (season_id,),
        )
        count_row = count_cursor.fetchone()
        result.data_available = int((count_row or {}).get("MatchCount") or 0) > 0
        if not result.data_available:
            result.standings = []
            result.winner_id = None

        result.standings = arrange_standings(result.standings)
        return result

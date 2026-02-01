from typing import Optional, List

from schemas.season_schema import (
    SeasonCreateRequest,
    SeasonUpdateRequest,
    SeasonResponse,
    LeagueTableResponse,
    LeagueTableStanding
)


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
            logo_url=row["LogoUrl"]
        )

    def create(self, request: SeasonCreateRequest) -> SeasonResponse:
        query = """
            INSERT INTO tblSeasons (Name, StartDate, EndDate, LogoUrl)
            VALUES (%s, %s, %s, %s)
        """

        cursor = self.db.cursor()
        cursor.execute(
            query,
            (
                request.name,
                request.start_date,
                request.end_date,
                str(request.logo_url)
            )
        )

        season_id = cursor.lastrowid
        return self.get_by_id(season_id)

    def get_by_id(self, season_id: int) -> Optional[SeasonResponse]:
        query = """
            SELECT Id, Name, StartDate, EndDate, LogoUrl
            FROM tblSeasons
            WHERE Id = %s AND Void = 0
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (season_id,))
        row = cursor.fetchone()

        return self._map_row_to_schema(row)

    def get_all(self) -> List[SeasonResponse]:
        query = """
            SELECT Id, Name, StartDate, EndDate, LogoUrl
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
            "logo_url": "LogoUrl"
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
    
    def get_league_table(self, season_id: int) -> LeagueTableResponse:
        cursor = self.db.cursor(dictionary=True)

        cursor.callproc("usp_GetLeagueTable", [season_id])

        result = LeagueTableResponse(standings=[])
        for res in cursor.stored_results():
            rows = res.fetchall()
            for row in rows:
                result.standings.append(
                    LeagueTableStanding(
                        team_id=row["TeamId"],
                        team_name=row["TeamName"],
                        matches_played=row.get("MatchesPlayed", 0),
                        wins=row.get("Wins", 0),
                        points=row.get("Points", 0),
                        net_points=row.get("NetPoints", 0),
                        head_to_head_wins=row.get("HeadToHeadWins", 0),
                    )
                )

        return result
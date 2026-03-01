from typing import Optional, List
from schemas.player_schema import (
    PlayerCreateRequest,
    PlayerUpdateRequest,
    PlayerResponse,
    PlayerProfileResponse
)


class PlayerRepository:

    def __init__(self, db):
        self.db = db

    def get_all(self) -> List[PlayerResponse]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute("""
            SELECT Id, FirstName, LastName, DateOfBirth,
                   AvatarUrl, NationalityId
            FROM tblPlayers
            WHERE Void = 0
        """)
        rows = cursor.fetchall()

        return [
            PlayerResponse(
                id=row["Id"],
                first_name=row["FirstName"],
                last_name=row["LastName"],
                date_of_birth=row["DateOfBirth"],
                avatar_url=row["AvatarUrl"],
                nationality_id=row["NationalityId"]
            )
            for row in rows
        ]

    def get_profiles(self) -> List[PlayerProfileResponse]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT
                p.Id,
                p.FirstName,
                p.LastName,
                p.DateOfBirth,
                p.AvatarUrl,
                p.NationalityId,
                debut.DebutSeasonId,
                debut.DebutSeasonName,
                IFNULL(stats.TotalCoinsPocketed, 0) AS TotalCoinsPocketed,
                IFNULL(stats.TotalCoinsFined, 0) AS TotalCoinsFined,
                IFNULL(stats.TotalStrikersPocketed, 0) AS TotalStrikersPocketed
            FROM tblPlayers p
            LEFT JOIN (
                SELECT
                    pst.PlayerId,
                    s.Id AS DebutSeasonId,
                    s.Name AS DebutSeasonName
                FROM tblPlayersSeasonsTeams pst
                INNER JOIN (
                    SELECT PlayerId, MIN(SeasonId) AS DebutSeasonId
                    FROM tblPlayersSeasonsTeams
                    WHERE Void = 0
                    GROUP BY PlayerId
                ) d
                    ON d.PlayerId = pst.PlayerId
                   AND d.DebutSeasonId = pst.SeasonId
                INNER JOIN tblSeasons s
                    ON s.Id = pst.SeasonId
                   AND s.Void = 0
                WHERE pst.Void = 0
            ) debut
                ON debut.PlayerId = p.Id
            LEFT JOIN (
                SELECT
                    ms.PlayerId,
                    SUM(IFNULL(ms.CoinsPocketed, 0)) AS TotalCoinsPocketed,
                    SUM(IFNULL(ms.CoinsFined, 0)) AS TotalCoinsFined,
                    SUM(IFNULL(ms.StrikersPocketed, 0)) AS TotalStrikersPocketed
                FROM tblMatchStats ms
                INNER JOIN tblMatches m
                    ON m.Id = ms.MatchId
                WHERE ms.Void = 0
                  AND m.Void = 0
                  AND m.Status = 3
                GROUP BY ms.PlayerId
            ) stats
                ON stats.PlayerId = p.Id
            WHERE p.Void = 0
            ORDER BY p.FirstName ASC, p.LastName ASC
            """
        )
        rows = cursor.fetchall()

        return [
            PlayerProfileResponse(
                id=row["Id"],
                first_name=row["FirstName"],
                last_name=row["LastName"],
                date_of_birth=row["DateOfBirth"],
                avatar_url=row["AvatarUrl"],
                nationality_id=row["NationalityId"],
                debut_season_id=row.get("DebutSeasonId"),
                debut_season_name=row.get("DebutSeasonName"),
                total_coins_pocketed=row.get("TotalCoinsPocketed", 0),
                total_coins_fined=row.get("TotalCoinsFined", 0),
                total_strikers_pocketed=row.get("TotalStrikersPocketed", 0),
            )
            for row in rows
        ]

    def get_by_id(self, player_id: int) -> Optional[PlayerResponse]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute("""
            SELECT Id, FirstName, LastName, DateOfBirth,
                   AvatarUrl, NationalityId
            FROM tblPlayers
            WHERE Id = %s AND Void = 0
        """, (player_id,))
        row = cursor.fetchone()

        if not row:
            return None

        return PlayerResponse(
            id=row["Id"],
            first_name=row["FirstName"],
            last_name=row["LastName"],
            date_of_birth=row["DateOfBirth"],
            avatar_url=row["AvatarUrl"],
            nationality_id=row["NationalityId"]
        )

    def create(self, request: PlayerCreateRequest) -> PlayerResponse:
        cursor = self.db.cursor()

        query = """
            INSERT INTO tblPlayers
            (FirstName, LastName, DateOfBirth, AvatarUrl, NationalityId)
            VALUES (%s, %s, %s, %s, %s)
        """

        cursor.execute(query, (
            request.first_name,
            request.last_name,
            request.date_of_birth,
            str(request.avatar_url),
            request.nationality_id
        ))

        self.db.commit()
        player_id = cursor.lastrowid

        return self.get_by_id(player_id)

    def update(self, player_id: int, request: PlayerUpdateRequest) -> Optional[PlayerResponse]:
        update_data = request.model_dump(exclude_unset=True)

        if not update_data:
            return self.get_by_id(player_id)

        field_mapping = {
            "first_name": "FirstName",
            "last_name": "LastName",
            "date_of_birth": "DateOfBirth",
            "avatar_url": "AvatarUrl",
            "nationality_id": "NationalityId"
        }

        fields = []
        values = []

        for key, value in update_data.items():
            if key in field_mapping:
                fields.append(f"{field_mapping[key]} = %s")
                values.append(str(value) if key == "avatar_url" else value)

        if not fields:
            return self.get_by_id(player_id)

        query = f"""
            UPDATE tblPlayers
            SET {', '.join(fields)}
            WHERE Id = %s AND Void = 0
        """
        values.append(player_id)
        

        cursor = self.db.cursor()
        cursor.execute(query, tuple(values))
        self.db.commit()

        return self.get_by_id(player_id)

    def delete(self, player_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute("""
            UPDATE tblPlayers
            SET Void = 1
            WHERE Id = %s AND Void = 0
        """, (player_id,))
        self.db.commit()

        return cursor.rowcount > 0

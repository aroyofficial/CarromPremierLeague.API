from typing import Optional, List
from schemas.match_schema import (
    MatchCreateRequest,
    MatchUpdateRequest,
    MatchResponse
)


class MatchRepository:

    def __init__(self, db):
        self.db = db

    @staticmethod
    def _map(row) -> Optional[MatchResponse]:
        if not row:
            return None

        golden = False
        if row["GoldenStrike"] is not None:
            if isinstance(row["GoldenStrike"], (bytes, bytearray)):
                golden = bool(row["GoldenStrike"][0])
            else:
                golden = bool(row["GoldenStrike"])

        return MatchResponse(
            id=row["Id"],
            team1=row["Team1"],
            team2=row["Team2"],
            scheduled_date=row["ScheduledDate"],
            duration=row["Duration"],
            extra=row["Extra"],
            golden_strike=golden,
            category=row["Category"],
            status=row["Status"],
            season_id=row["SeasonId"],
            net_points=row["NetPoints"],
            outcome=row["Outcome"],
        )
        
    def create(self, request: MatchCreateRequest) -> MatchResponse:
        query = """
            INSERT INTO tblMatches (
                Team1,
                Team2,
                ScheduledDate,
                Duration,
                Extra,
                GoldenStrike,
                Category,
                Status,
                `Order`,
                SeasonId,
                NetPoints,
                Outcome
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        cursor = self.db.cursor()

        cursor.execute(
            query,
            (
                request.team1,
                request.team2,
                request.scheduled_date,
                request.duration,              # None → becomes NULL
                request.extra,                 # None → becomes NULL
                int(request.golden_strike) if request.golden_strike is not None else 0,
                request.category,
                request.status,
                request.order,                 # None → NULL
                request.season_id,
                request.net_points,            # None → NULL
                request.outcome                # None → NULL
            )
        )

        self.db.commit()

        match_id = cursor.lastrowid
        return self.get_by_id(match_id)
    
    def get_by_id(self, match_id: int) -> Optional[MatchResponse]:
        query = """
            SELECT *
            FROM tblMatches
            WHERE Id = %s AND Void = 0
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (match_id,))
        return self._map(cursor.fetchone())

    def get_all(self, season_id: int = None) -> List[MatchResponse]:
        query = """
            SELECT *
            FROM tblMatches
            WHERE Void = 0
        """

        if season_id is not None:
            query += f" AND SeasonId = {season_id}"

        query += " ORDER BY ScheduledDate DESC"

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query)
        rows = cursor.fetchall()

        return [self._map(row) for row in rows]

    def update(self, match_id: int, request: MatchUpdateRequest) -> Optional[MatchResponse]:
        update_data = request.model_dump(exclude_unset=True)

        if not update_data:
            return self.get_by_id(match_id)

        field_mapping = {
            "scheduled_date": "ScheduledDate",
            "duration": "Duration",
            "extra": "Extra",
            "golden_strike": "GoldenStrike",
            "status": "Status",
            "net_points": "NetPoints",
            "outcome": "Outcome"
        }

        fields = []
        values = []

        for key, value in update_data.items():
            if key in field_mapping:
                fields.append(f"{field_mapping[key]} = %s")
                values.append(value)

        query = f"""
            UPDATE tblMatches
            SET {', '.join(fields)}
            WHERE Id = %s AND Void = 0
        """

        values.append(match_id)

        cursor = self.db.cursor()
        cursor.execute(query, tuple(values))
        self.db.commit()

        return self.get_by_id(match_id)

    def soft_delete(self, match_id: int) -> bool:
        query = """
            UPDATE tblMatches
            SET Void = 1
            WHERE Id = %s
        """

        cursor = self.db.cursor()
        cursor.execute(query, (match_id,))
        self.db.commit()

        return cursor.rowcount > 0
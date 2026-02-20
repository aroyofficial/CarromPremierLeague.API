from typing import Optional, List
from enums.match_category import MatchCategory
from schemas.match_schema import (
    MatchCreateRequest,
    MatchUpdateRequest,
    MatchResponse,
    MatchOrderResponse,
    MatchStatUpsertItem,
    MatchStatResponse
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
            order=row["Order"],
            toss_outcome=row["TossOutcome"]
        )

    @staticmethod
    def _map_stat(row) -> Optional[MatchStatResponse]:
        if not row:
            return None

        return MatchStatResponse(
            match_id=row["MatchId"],
            player_id=row["PlayerId"],
            coins_pocketed=row["CoinsPocketed"],
            strikers_pocketed=row["StrikersPocketed"],
            coins_fined=row["CoinsFined"],
            shots_taken=row["ShotsTaken"]
        )

    def _all_league_matches_completed(self, season_id: int) -> bool:
        query = """
            SELECT
                COUNT(*) AS TotalLeagueMatches,
                SUM(CASE WHEN Status <> 3 THEN 1 ELSE 0 END) AS PendingLeagueMatches
            FROM tblMatches
            WHERE SeasonId = %s
              AND Category = %s
              AND Void = 0
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (season_id, MatchCategory.League.value))
        row = cursor.fetchone()
        if not row:
            return False

        total = int(row.get("TotalLeagueMatches", 0) or 0)
        pending = int(row.get("PendingLeagueMatches", 0) or 0)
        return total > 0 and pending == 0

    def _get_top_two_teams_from_league_table(self, season_id: int) -> List[int]:
        cursor = self.db.cursor(dictionary=True)
        cursor.callproc("usp_GetLeagueTable", [season_id])

        league_rows = []
        result_sets = cursor.stored_results()
        try:
            league_result = next(result_sets)
            league_rows = league_result.fetchall()
        except StopIteration:
            return []

        ranked_team_ids = [
            int(row["TeamId"])
            for row in league_rows
            if row.get("TeamId") is not None and int(row.get("MatchesPlayed", 0) or 0) > 0
        ]

        if len(ranked_team_ids) < 2:
            return []

        return ranked_team_ids[:2]

    def _sync_final_match_teams(self, season_id: int) -> None:
        if not self._all_league_matches_completed(season_id):
            return

        top_two_team_ids = self._get_top_two_teams_from_league_table(season_id)
        if len(top_two_team_ids) < 2:
            return

        team1_id, team2_id = top_two_team_ids[0], top_two_team_ids[1]
        if team1_id == team2_id:
            return

        update_query = """
            UPDATE tblMatches
            SET Team1 = %s,
                Team2 = %s,
                UpdatedAt = CURRENT_TIMESTAMP(6)
            WHERE SeasonId = %s
              AND Category = %s
              AND Void = 0
              AND Status <> 3
            ORDER BY `Order` ASC
            LIMIT 1
        """

        cursor = self.db.cursor()
        cursor.execute(
            update_query,
            (team1_id, team2_id, season_id, MatchCategory.Final.value)
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

        if request.category == MatchCategory.Final:
            query1 = """
                SELECT 1
                FROM tblMatches
                WHERE SeasonId = %s
                AND Void = 0
                AND Category = %s
                LIMIT 1
            """

            cursor = self.db.cursor()
            cursor.execute(query1, (request.season_id, request.category.value))
            row = cursor.fetchone()
            if row is not None:
                raise ValueError("Final match already exists")

        cursor = self.db.cursor()

        cursor.execute(
            query,
            (
                request.team1,
                request.team2,
                request.scheduled_date,
                request.duration,              
                request.extra,                 
                int(request.golden_strike) if request.golden_strike is not None else 0,
                request.category.value,
                request.status.value,
                request.order,                 
                request.season_id,
                request.net_points,            
                request.outcome.value                
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

        query += " ORDER BY `Order` ASC"

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query)
        rows = cursor.fetchall()

        return [self._map(row) for row in rows]

    def update(self, match_id: int, request: MatchUpdateRequest) -> Optional[MatchResponse]:
        update_data = request.model_dump(exclude_unset=True)

        if not update_data:
            return self.get_by_id(match_id)

        field_mapping = {
            "team1": "Team1",
            "team2": "Team2",
            "scheduled_date": "ScheduledDate",
            "duration": "Duration",
            "extra": "Extra",
            "golden_strike": "GoldenStrike",
            "status": "Status",
            "net_points": "NetPoints",
            "outcome": "Outcome",
            "toss_outcome": "TossOutcome"
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
            AND Status <> 3
        """

        values.append(match_id)

        cursor = self.db.cursor()
        cursor.execute(query, tuple(values))

        updated_match = self.get_by_id(match_id)
        if (
            updated_match is not None
            and updated_match.category == MatchCategory.League.value
            and updated_match.status == 3
        ):
            self._sync_final_match_teams(updated_match.season_id)

        self.db.commit()

        return updated_match

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
    
    def get_order(self, season_id: int) -> MatchOrderResponse:
        query = """
            SELECT *
            FROM (
                SELECT COALESCE(MAX(tm.`Order`), 0) + 1 AS DesiredOrder
                FROM tblMatches tm
                WHERE tm.SeasonId = %s
                AND tm.Void = 0
            ) t
            WHERE EXISTS (
                SELECT 1
                FROM tblSeasons ts
                WHERE ts.Id = %s
                AND ts.Status <> 3
            );
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (season_id, season_id,))
        row = cursor.fetchone()

        if not row or row["DesiredOrder"] is None:
            raise ValueError("Season not found or already completed")
        return MatchOrderResponse(order=row["DesiredOrder"])

    def upsert_stats(self, match_id: int, stats: List[MatchStatUpsertItem]) -> List[MatchStatResponse]:
        query = """
            INSERT INTO tblMatchStats (
                MatchId,
                PlayerId,
                CoinsPocketed,
                StrikersPocketed,
                CoinsFined,
                ShotsTaken
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                CoinsPocketed = VALUES(CoinsPocketed),
                StrikersPocketed = VALUES(StrikersPocketed),
                CoinsFined = VALUES(CoinsFined),
                ShotsTaken = VALUES(ShotsTaken),
                Void = 0,
                UpdatedAt = CURRENT_TIMESTAMP(6)
        """

        values = [
            (
                match_id,
                item.player_id,
                item.coins_pocketed,
                item.strikers_pocketed,
                item.coins_fined,
                item.shots_taken
            )
            for item in stats
        ]

        cursor = self.db.cursor()
        cursor.executemany(query, values)
        self.db.commit()

        return self.get_stats(match_id)

    def get_stats(self, match_id: int) -> List[MatchStatResponse]:
        query = """
            SELECT MatchId, PlayerId, CoinsPocketed, StrikersPocketed, CoinsFined, ShotsTaken
            FROM tblMatchStats
            WHERE MatchId = %s
              AND Void = 0
            ORDER BY PlayerId ASC
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (match_id,))
        rows = cursor.fetchall()

        return [self._map_stat(row) for row in rows]

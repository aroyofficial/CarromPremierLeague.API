from typing import List
from schemas.stats_schema import HeadToHeadResponse, TopCoinPotterResponse
from enums.match_category import MatchCategory
from enums.match_status import MatchStatus


class StatsRepository:

    def __init__(self, db):
        self.db = db

    def get_head_to_head(self, team1_id: int, team2_id: int) -> HeadToHeadResponse:
        cursor = self.db.cursor(dictionary=True)

        cursor.callproc("usp_GetLifetimeHeadToHead", [team1_id, team2_id])

        for result in cursor.stored_results():
            row = result.fetchone()

            if not row:
                return HeadToHeadResponse(
                    team1_id=team1_id,
                    team2_id=team2_id,
                    matches_played=0,
                    team1_wins=0,
                    team2_wins=0,
                    team1_net_points=0,
                    team2_net_points=0
                )

            return HeadToHeadResponse(
                team1_id=row["TeamAId"],
                team2_id=row["TeamBId"],
                matches_played=row.get("TotalMatches", 0),
                team1_wins=row.get("TeamAWins", 0),
                team2_wins=row.get("TeamBWins", 0),
                team1_net_points=row.get("TeamANetPoints", 0),
                team2_net_points=row.get("TeamBNetPoints", 0),
            )

    def get_season_top_coin_potters(
        self,
        season_id: int,
        limit: int = 3
    ) -> List[TopCoinPotterResponse]:
        cursor = self.db.cursor(dictionary=True)
        cursor.callproc("usp_GetSeasonTopCoinPotters", [season_id, limit])

        top_coin_potters = []
        for result in cursor.stored_results():
            rows = result.fetchall()
            for row in rows:
                top_coin_potters.append(
                    TopCoinPotterResponse(
                        player_id=row["PlayerId"],
                        first_name=row["FirstName"],
                        last_name=row["LastName"],
                        avatar_url=row.get("AvatarUrl"),
                        team_id=row.get("TeamId"),
                        team_name=row.get("TeamName"),
                        coins_pocketed=row.get("CoinsPocketed", 0),
                        coins_fined=row.get("CoinsFined", 0),
                        strikers_pocketed=row.get("StrikersPocketed", 0),
                    )
                )
            break

        return top_coin_potters

    def has_completed_league_matches(self, season_id: int) -> bool:
        query = """
            SELECT 1
            FROM tblMatches
            WHERE SeasonId = %s
              AND Category = %s
              AND Status = %s
              AND Void = 0
            LIMIT 1
        """

        cursor = self.db.cursor()
        cursor.execute(
            query,
            (
                season_id,
                MatchCategory.League.value,
                MatchStatus.Played.value
            )
        )
        return cursor.fetchone() is not None

    def is_league_stage_completed(self, season_id: int) -> bool:
        query = """
            SELECT
                COUNT(*) AS TotalLeagueMatches,
                SUM(CASE WHEN Status <> %s THEN 1 ELSE 0 END) AS PendingLeagueMatches
            FROM tblMatches
            WHERE SeasonId = %s
              AND Category = %s
              AND Void = 0
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            query,
            (
                MatchStatus.Played.value,
                season_id,
                MatchCategory.League.value,
            ),
        )
        row = cursor.fetchone()
        if not row:
            return False

        total = int(row.get("TotalLeagueMatches", 0) or 0)
        pending = int(row.get("PendingLeagueMatches", 0) or 0)
        return total > 0 and pending == 0

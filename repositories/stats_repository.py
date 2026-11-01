from typing import List
from schemas.stats_schema import (
    HeadToHeadResponse,
    PlayerSeasonStatResponse,
    SeasonPlayerStatsResponse,
    TopCoinPotterResponse,
)
from enums.match_category import MatchCategory
from enums.match_status import MatchStatus


class StatsRepository:

    def __init__(self, db):
        self.db = db

    def get_head_to_head(self, team1_id: int, team2_id: int) -> HeadToHeadResponse:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute("EXEC dbo.usp_GetLifetimeHeadToHead ?, ?", (team1_id, team2_id))
        row = cursor.fetchone()
        if not row:
            return HeadToHeadResponse(
                team1_id=team1_id, team2_id=team2_id, matches_played=0,
                team1_wins=0, team2_wins=0, team1_net_points=0, team2_net_points=0
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
        cursor.execute("EXEC dbo.usp_GetSeasonTopCoinPotters ?, ?", (season_id, limit))
        top_coin_potters = []
        for row in cursor.fetchall():
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

        return top_coin_potters

    def get_season_player_stats(self, season_id: int) -> SeasonPlayerStatsResponse:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT COUNT(*) AS MatchCount
            FROM tblMatches
            WHERE SeasonId = %s AND Void = 0
            """,
            (season_id,),
        )
        count_row = cursor.fetchone()
        if int((count_row or {}).get("MatchCount") or 0) == 0:
            return SeasonPlayerStatsResponse(data_available=False, players=[])

        cursor.execute(
            """
            SELECT
                p.Id AS PlayerId,
                p.FirstName,
                p.LastName,
                p.AvatarUrl,
                roster.TeamId,
                roster.TeamName,
                ISNULL(stats.MatchesPlayed, 0) AS MatchesPlayed,
                ISNULL(stats.CoinsPocketed, 0) AS CoinsPocketed,
                ISNULL(stats.OpponentCoinsPocketed, 0) AS OpponentCoinsPocketed,
                ISNULL(stats.StrikersPocketed, 0) AS StrikersPocketed,
                ISNULL(stats.SingleFines, 0) AS SingleFines,
                ISNULL(stats.DoubleFines, 0) AS DoubleFines,
                ISNULL(stats.CoinsFined, 0) AS CoinsFined
            FROM tblPlayers p
            INNER JOIN (
                SELECT pst.PlayerId, pst.TeamId, t.Name AS TeamName
                FROM tblPlayersSeasonsTeams pst
                LEFT JOIN tblTeams t
                    ON t.Id = pst.TeamId
                   AND t.Void = 0
                WHERE pst.SeasonId = %s
                  AND pst.Void = 0
            ) roster
                ON roster.PlayerId = p.Id
            LEFT JOIN (
                SELECT
                    ms.PlayerId,
                    COUNT(DISTINCT m.Id) AS MatchesPlayed,
                    SUM(ISNULL(ms.CoinsPocketed, 0)) AS CoinsPocketed,
                    SUM(ISNULL(ms.OpponentCoinsPocketed, 0)) AS OpponentCoinsPocketed,
                    SUM(ISNULL(ms.StrikersPocketed, 0)) AS StrikersPocketed,
                    SUM(ISNULL(ms.SingleFines, 0)) AS SingleFines,
                    SUM(ISNULL(ms.DoubleFines, 0)) AS DoubleFines,
                    SUM(ISNULL(ms.CoinsFined, 0)) AS CoinsFined
                FROM tblMatchStats ms
                INNER JOIN tblMatches m
                    ON m.Id = ms.MatchId
                   AND m.Void = 0
                   AND m.Status = %s
                   AND m.SeasonId = %s
                WHERE ms.Void = 0
                GROUP BY ms.PlayerId
            ) stats
                ON stats.PlayerId = p.Id
            WHERE p.Void = 0
            ORDER BY p.FirstName ASC, p.LastName ASC
            """,
            (season_id, MatchStatus.Played.value, season_id),
        )

        players = [
            PlayerSeasonStatResponse(
                player_id=row["PlayerId"],
                first_name=row["FirstName"],
                last_name=row["LastName"],
                avatar_url=row.get("AvatarUrl"),
                team_id=row.get("TeamId"),
                team_name=row.get("TeamName"),
                matches_played=int(row.get("MatchesPlayed") or 0),
                coins_pocketed=int(row.get("CoinsPocketed") or 0),
                opponent_coins_pocketed=int(row.get("OpponentCoinsPocketed") or 0),
                strikers_pocketed=int(row.get("StrikersPocketed") or 0),
                single_fines=int(row.get("SingleFines") or 0),
                double_fines=int(row.get("DoubleFines") or 0),
                coins_fined=int(row.get("CoinsFined") or 0),
            )
            for row in cursor.fetchall()
        ]
        return SeasonPlayerStatsResponse(data_available=True, players=players)

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

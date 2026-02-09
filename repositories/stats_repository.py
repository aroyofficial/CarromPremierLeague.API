from schemas.stats_schema import HeadToHeadResponse


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

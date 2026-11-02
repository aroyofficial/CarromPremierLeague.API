from datetime import date
from itertools import combinations
from typing import Optional, List
from enums.match_category import MatchCategory
from enums.match_status import MatchStatus
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
            toss_outcome=row["TossOutcome"],
            started_at=row.get("StartedAt"),
            extra_started_at=row.get("ExtraStartedAt"),
        )

    @staticmethod
    def _map_stat(row) -> Optional[MatchStatResponse]:
        if not row:
            return None

        return MatchStatResponse(
            match_id=row["MatchId"],
            player_id=row["PlayerId"],
            coins_pocketed=row["CoinsPocketed"],
            opponent_coins_pocketed=int(row.get("OpponentCoinsPocketed") or 0),
            single_fines=int(row.get("SingleFines") or 0),
            double_fines=int(row.get("DoubleFines") or 0),
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

    def _get_ranked_league_table(self, season_id: int) -> List[dict]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute("EXEC dbo.usp_GetLeagueTable ?", (season_id,))
        rows = cursor.fetchall()
        while cursor.nextset():
            pass
        return rows

    def _participant_count(self, season_id: int) -> int:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT COUNT(*) AS ParticipantCount
            FROM tblSeasonsTeams
            WHERE SeasonId = %s
              AND Void = 0
            """,
            (season_id,),
        )
        row = cursor.fetchone() or {}
        return int(row.get("ParticipantCount") or 0)

    def _playoff_date(self, season_id: int) -> date:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT COALESCE(EndDate, StartDate) AS PlayoffDate
            FROM tblSeasons
            WHERE Id = %s
              AND Void = 0
            """,
            (season_id,),
        )
        row = cursor.fetchone() or {}
        return row.get("PlayoffDate") or date.today()

    @staticmethod
    def _tie_key(row: dict) -> tuple[int, int, int, int]:
        return (
            int(row.get("Points") or 0),
            int(row.get("TotalNetPoints") or 0),
            int(row.get("HeadToHeadWins") or 0),
            int(row.get("GoldenStrikeWins") or 0),
        )

    def _qualification_tie_groups(self, ranked_rows: List[dict], participant_count: int) -> List[List[int]]:
        # Four teams: a tie on either side of #1 or #2 is unresolved, so those
        # teams play Golden Strike before the final is assigned.
        # Larger leagues: the same applies to #1 and to the eliminator places
        # (#2 and #3), including the cut with the first team that misses out.
        # Seed the four-team knockout bracket deterministically. For a larger
        # league, a tie across the qualification cut (#4/#5) also needs a
        # Golden Strike before the bracket can be built.
        boundaries = [1, 2, 3] if participant_count == 4 else [1, 2, 3, 4]
        groups: list[list[int]] = []
        seen: set[tuple[int, ...]] = set()

        for boundary in boundaries:
            if boundary >= len(ranked_rows):
                continue
            if self._tie_key(ranked_rows[boundary - 1]) != self._tie_key(ranked_rows[boundary]):
                continue

            start = boundary - 1
            end = boundary
            while start > 0 and self._tie_key(ranked_rows[start - 1]) == self._tie_key(ranked_rows[boundary]):
                start -= 1
            while end + 1 < len(ranked_rows) and self._tie_key(ranked_rows[end + 1]) == self._tie_key(ranked_rows[boundary]):
                end += 1

            group = tuple(sorted(int(row["TeamId"]) for row in ranked_rows[start : end + 1]))
            if group not in seen:
                seen.add(group)
                groups.append(list(group))
        return groups

    def _has_pending_golden_strikes(self, season_id: int, team_ids: List[int]) -> bool:
        placeholders = ", ".join(["%s"] * len(team_ids))
        query = f"""
            SELECT COUNT(*) AS PendingCount
            FROM tblMatches
            WHERE SeasonId = %s
              AND Category = %s
              AND GoldenStrike = 1
              AND Status <> 3
              AND Void = 0
              AND Team1 IN ({placeholders})
              AND Team2 IN ({placeholders})
        """
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            query,
            (season_id, MatchCategory.GoldenStrike.value, *team_ids, *team_ids),
        )
        row = cursor.fetchone() or {}
        return int(row.get("PendingCount") or 0) > 0

    def _next_order(self, season_id: int) -> int:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT COALESCE(MAX([Order]), 0) + 1 AS NextOrder
            FROM tblMatches
            WHERE SeasonId = %s
              AND Void = 0
            """,
            (season_id,),
        )
        row = cursor.fetchone() or {}
        return int(row.get("NextOrder") or 1)

    def _schedule_golden_strike_round(self, season_id: int, team_ids: List[int]) -> None:
        cursor = self.db.cursor()
        order = self._next_order(season_id)
        scheduled_date = self._playoff_date(season_id)
        query = """
            INSERT INTO tblMatches (
                Team1, Team2, ScheduledDate, GoldenStrike, Category, Status,
                [Order], SeasonId, Outcome, TossOutcome
            )
            VALUES (%s, %s, %s, 1, %s, 1, %s, %s, 3, 3)
        """
        for team1_id, team2_id in combinations(team_ids, 2):
            cursor.execute(
                query,
                (
                    team1_id,
                    team2_id,
                    scheduled_date,
                    MatchCategory.GoldenStrike.value,
                    order,
                    season_id,
                ),
            )
            order += 1

    def _ensure_knockout_match(
        self,
        season_id: int,
        category: MatchCategory,
        team1_id: int,
        team2_id: Optional[int],
    ) -> Optional[dict]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT TOP (1) Id, Status, Team1, Team2, Outcome, [Order]
            FROM tblMatches
            WHERE SeasonId = %s
              AND Category = %s
              AND Void = 0
            ORDER BY [Order] ASC, Id ASC
            """,
            (season_id, category.value),
        )
        existing = cursor.fetchone()
        if existing:
            if (
                int(existing["Status"]) != 3
                and team1_id is not None
                and team2_id is not None
            ):
                cursor = self.db.cursor()
                cursor.execute(
                    """
                    UPDATE tblMatches
                    SET Team1 = %s,
                        Team2 = %s,
                        UpdatedAt = SYSUTCDATETIME()
                    WHERE Id = %s
                    """,
                    (team1_id, team2_id, existing["Id"]),
                )
                existing["Team1"] = team1_id
                existing["Team2"] = team2_id
            return existing

        if team1_id is None or team2_id is None:
            return None

        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblMatches (
                Team1, Team2, ScheduledDate, GoldenStrike, Category, Status,
                [Order], SeasonId, Outcome, TossOutcome
            )
            VALUES (%s, %s, %s, 0, %s, 1, %s, %s, 3, 3)
            """,
            (
                team1_id,
                team2_id,
                self._playoff_date(season_id),
                category.value,
                self._next_order(season_id),
                season_id,
            ),
        )

        if not cursor.lastrowid:
            return None
        created = self.get_by_id(cursor.lastrowid)
        return {
            "Id": created.id,
            "Status": created.status,
            "Team1": created.team1,
            "Team2": created.team2,
            "Outcome": created.outcome,
            "Order": created.order,
        } if created else None

    def _latest_golden_strike(
        self,
        season_id: int,
        team1_id: int,
        team2_id: int,
        after_order: int = 0,
    ) -> Optional[dict]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT TOP (1) Id, Status, Team1, Team2, Outcome, [Order]
            FROM tblMatches
            WHERE SeasonId = %s
              AND Category = %s
              AND Void = 0
              AND [Order] > %s
              AND (
                    (Team1 = %s AND Team2 = %s)
                    OR (Team1 = %s AND Team2 = %s)
              )
            ORDER BY [Order] DESC, Id DESC
            """,
            (
                season_id,
                MatchCategory.GoldenStrike.value,
                after_order,
                team1_id,
                team2_id,
                team2_id,
                team1_id,
            ),
        )
        return cursor.fetchone()

    def _knockout_winner(self, season_id: int, match: Optional[dict]) -> Optional[int]:
        if not match or int(match.get("Status") or 0) != 3:
            return None
        if match.get("Team1") is None or match.get("Team2") is None:
            return None

        outcome = int(match.get("Outcome") or 3)
        if outcome == 1:
            return int(match["Team1"])
        if outcome == 2:
            return int(match["Team2"])
        if outcome != 3:
            return None

        golden_strike = self._latest_golden_strike(
            season_id,
            int(match["Team1"]),
            int(match["Team2"]),
            int(match.get("Order") or 0),
        )
        if not golden_strike or int(golden_strike.get("Status") or 0) != 3:
            return None
        golden_outcome = int(golden_strike.get("Outcome") or 3)
        if golden_outcome == 1 and golden_strike.get("Team1") is not None:
            return int(golden_strike["Team1"])
        if golden_outcome == 2 and golden_strike.get("Team2") is not None:
            return int(golden_strike["Team2"])
        return None

    def _schedule_knockout_draw_golden_strikes(self, season_id: int) -> None:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT Team1, Team2, [Order]
            FROM tblMatches
            WHERE SeasonId = %s
              AND Category IN (%s, %s, %s, %s)
              AND Status = 3
              AND Outcome = 3
              AND Extra IS NOT NULL
              AND Extra > 0
              AND Team1 IS NOT NULL
              AND Team2 IS NOT NULL
              AND Void = 0
            """,
            (
                season_id,
                MatchCategory.Eliminator.value,
                MatchCategory.Qualifier1.value,
                MatchCategory.Qualifier2.value,
                MatchCategory.Final.value,
            ),
        )
        drawn_matches = cursor.fetchall()
        for match in drawn_matches:
            team1_id = int(match["Team1"])
            team2_id = int(match["Team2"])
            if self._latest_golden_strike(
                season_id,
                team1_id,
                team2_id,
                int(match.get("Order") or 0),
            ):
                continue
            self._schedule_golden_strike_round(season_id, [team1_id, team2_id])

    def _has_pending_season_golden_strikes(self, season_id: int) -> bool:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT COUNT(*) AS PendingCount
            FROM tblMatches
            WHERE SeasonId = %s
              AND Category = %s
              AND Status <> 3
              AND Void = 0
            """,
            (season_id, MatchCategory.GoldenStrike.value),
        )
        return int((cursor.fetchone() or {}).get("PendingCount") or 0) > 0

    def _sync_playoff_schedule(self, season_id: int) -> None:
        # Golden Strike is an operator decision inside the finished knockout
        # match. A declined Golden Strike must not create another fixture.
        if self._has_pending_season_golden_strikes(season_id):
            return
        if not self._all_league_matches_completed(season_id):
            return

        participant_count = self._participant_count(season_id)
        if participant_count < 4:
            return

        ranked_rows = [
            row
            for row in self._get_ranked_league_table(season_id)
            if int(row.get("MatchesPlayed") or 0) > 0
        ]
        if len(ranked_rows) < 2:
            return

        tie_groups = self._qualification_tie_groups(ranked_rows, participant_count)
        if tie_groups:
            for team_ids in tie_groups:
                if self._has_pending_golden_strikes(season_id, team_ids):
                    return
            for team_ids in tie_groups:
                self._schedule_golden_strike_round(season_id, team_ids)
            return

        ranked_team_ids = [int(row["TeamId"]) for row in ranked_rows]
        if len(ranked_team_ids) < 4:
            return

        qualifier1 = self._ensure_knockout_match(
            season_id,
            MatchCategory.Qualifier1,
            ranked_team_ids[0],
            ranked_team_ids[1],
        )
        eliminator = self._ensure_knockout_match(
            season_id,
            MatchCategory.Eliminator,
            ranked_team_ids[2],
            ranked_team_ids[3],
        )
        qualifier1_winner = self._knockout_winner(season_id, qualifier1)
        eliminator_winner = self._knockout_winner(season_id, eliminator)
        if qualifier1_winner is None or eliminator_winner is None:
            return

        qualifier1_loser = (
            int(qualifier1["Team2"])
            if qualifier1_winner == int(qualifier1["Team1"])
            else int(qualifier1["Team1"])
        )
        qualifier2 = self._ensure_knockout_match(
            season_id,
            MatchCategory.Qualifier2,
            qualifier1_loser,
            eliminator_winner,
        )
        qualifier2_winner = self._knockout_winner(season_id, qualifier2)
        if qualifier2_winner is None:
            return

        self._ensure_knockout_match(
            season_id,
            MatchCategory.Final,
            qualifier1_winner,
            qualifier2_winner,
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

        if update_data.get("status") == MatchStatus.InProgress.value:
            fields.append("StartedAt = COALESCE(StartedAt, SYSUTCDATETIME())")
        if update_data.get("extra") is not None:
            fields.append("ExtraStartedAt = COALESCE(ExtraStartedAt, SYSUTCDATETIME())")

        query = f"""
            UPDATE tblMatches
            SET {', '.join(fields)}
            WHERE Id = %s AND Void = 0
        """

        values.append(match_id)

        cursor = self.db.cursor()
        cursor.execute(query, tuple(values))

        updated_match = self.get_by_id(match_id)
        if (
            updated_match is not None
            and updated_match.status == 3
            and updated_match.category
            in (
                MatchCategory.League.value,
                MatchCategory.GoldenStrike.value,
                MatchCategory.Eliminator.value,
                MatchCategory.Qualifier1.value,
                MatchCategory.Qualifier2.value,
                MatchCategory.Final.value,
            )
        ):
            self._sync_playoff_schedule(updated_match.season_id)

        self.db.commit()

        return updated_match

    def reset(self, match_id: int) -> Optional[MatchResponse]:
        cursor = self.db.cursor()
        cursor.execute("DELETE FROM tblMatchStats WHERE MatchId = %s", (match_id,))
        cursor.execute(
            """
            UPDATE tblMatches
            SET Status = %s,
                Outcome = %s,
                TossOutcome = %s,
                NetPoints = NULL,
                Duration = NULL,
                Extra = NULL,
                GoldenStrike = 0,
                StartedAt = NULL,
                ExtraStartedAt = NULL
            WHERE Id = %s AND Void = 0
            """,
            (MatchStatus.Scheduled.value, 3, 3, match_id),
        )
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
                AND ts.Void = 0
                AND (ts.EndDate IS NULL OR ts.EndDate >= CURDATE())
            );
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (season_id, season_id,))
        row = cursor.fetchone()

        if not row or row["DesiredOrder"] is None:
            raise ValueError("Season not found or already completed")
        return MatchOrderResponse(order=row["DesiredOrder"])

    def upsert_stats(self, match_id: int, stats: List[MatchStatUpsertItem]) -> List[MatchStatResponse]:
        if not stats:
            return self.get_stats(match_id)
        cursor = self.db.cursor()
        # One statement for the whole scorecard, so the nine-coin trigger sees the final state
        # instead of a half-applied one.
        rows = ", ".join(["(?, ?, ?, ?, ?, ?, ?, ?, ?)"] * len(stats))
        query = f"""
            MERGE dbo.tblMatchStats AS target
            USING (VALUES {rows}) AS source
                (MatchId, PlayerId, CoinsPocketed, OpponentCoinsPocketed, SingleFines, DoubleFines,
                 StrikersPocketed, CoinsFined, ShotsTaken)
            ON target.MatchId = source.MatchId AND target.PlayerId = source.PlayerId
            WHEN MATCHED THEN UPDATE SET
                CoinsPocketed = source.CoinsPocketed,
                OpponentCoinsPocketed = source.OpponentCoinsPocketed,
                SingleFines = source.SingleFines,
                DoubleFines = source.DoubleFines,
                StrikersPocketed = source.StrikersPocketed,
                CoinsFined = source.CoinsFined,
                ShotsTaken = source.ShotsTaken,
                Void = 0,
                UpdatedAt = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT
                (MatchId, PlayerId, CoinsPocketed, OpponentCoinsPocketed, SingleFines, DoubleFines,
                 StrikersPocketed, CoinsFined, ShotsTaken)
                VALUES
                (source.MatchId, source.PlayerId, source.CoinsPocketed, source.OpponentCoinsPocketed,
                 source.SingleFines, source.DoubleFines, source.StrikersPocketed, source.CoinsFined, source.ShotsTaken);
        """
        values = []
        for item in stats:
            values.extend(
                (
                    match_id,
                    item.player_id,
                    item.coins_pocketed,
                    item.opponent_coins_pocketed,
                    item.single_fines,
                    item.double_fines,
                    item.strikers_pocketed,
                    item.coins_fined,
                    item.shots_taken,
                )
            )
        cursor.execute(query, tuple(values))
        self.db.commit()

        return self.get_stats(match_id)

    def players_on_match_teams(self, match_id: int) -> list[dict]:
        query = """
            SELECT pst.PlayerId, pst.TeamId
            FROM tblMatches m
            JOIN tblPlayersSeasonsTeams pst
              ON pst.SeasonId = m.SeasonId
             AND pst.Void = 0
             AND pst.TeamId IN (m.Team1, m.Team2)
            WHERE m.Id = %s
              AND m.Void = 0
        """
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (match_id,))
        return cursor.fetchall()

    def player_coin_lines(self, match_id: int) -> list[dict]:
        query = """
            SELECT ms.PlayerId,
                   pst.TeamId,
                   ISNULL(ms.CoinsPocketed, 0) AS CoinsPocketed,
                   ISNULL(ms.OpponentCoinsPocketed, 0) AS OpponentCoinsPocketed,
                   ISNULL(ms.SingleFines, 0) AS SingleFines,
                   ISNULL(ms.DoubleFines, 0) AS DoubleFines
            FROM tblMatchStats ms
            JOIN tblMatches m ON m.Id = ms.MatchId
            JOIN tblPlayersSeasonsTeams pst
              ON pst.PlayerId = ms.PlayerId
             AND pst.SeasonId = m.SeasonId
             AND pst.Void = 0
             AND pst.TeamId IN (m.Team1, m.Team2)
            WHERE ms.MatchId = %s
              AND ms.Void = 0
        """
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (match_id,))
        return cursor.fetchall()

    def get_stats(self, match_id: int) -> List[MatchStatResponse]:
        query = """
            SELECT MatchId, PlayerId, CoinsPocketed, OpponentCoinsPocketed, SingleFines, DoubleFines,
                   StrikersPocketed, CoinsFined, ShotsTaken
            FROM tblMatchStats
            WHERE MatchId = %s
              AND Void = 0
            ORDER BY PlayerId ASC
        """

        cursor = self.db.cursor(dictionary=True)
        cursor.execute(query, (match_id,))
        rows = cursor.fetchall()

        return [self._map_stat(row) for row in rows]

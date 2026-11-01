import copy
import math
from typing import Optional

from datetime import datetime

from enums.match_category import MatchCategory
from enums.match_status import MatchStatus
from schemas.schedule_schema import (
    ScheduleHeadToHead,
    ScheduleMatch,
    SchedulePlayerStat,
    ScheduleRankChange,
    ScheduleScoreDistribution,
    ScheduleWinProbability,
    SeasonScheduleResponse,
)


def _as_utc_iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    text = str(value).replace(" ", "T")
    return text if text.endswith("Z") else f"{text}Z"


def _as_bool(value) -> bool:
    if isinstance(value, (bytes, bytearray)):
        return bool(value[0]) if value else False
    return bool(value)


def _empty_record() -> dict:
    return {"points": 0, "net": 0, "wins_vs": {}, "played": 0}


def _fine_coins(player: SchedulePlayerStat) -> int:
    split = (player.single_fines or 0) + 2 * (player.double_fines or 0)
    if split:
        return split
    return player.coins_fined or 0


def _side_scores(players: list[SchedulePlayerStat], team1_id: int, team2_id: int) -> Optional[tuple[int, int]]:
    scores = {team1_id: 0, team2_id: 0}
    seen = {team1_id: False, team2_id: False}
    for player in players:
        if player.team_id not in scores:
            continue
        seen[player.team_id] = True
        scores[player.team_id] += (player.coins_pocketed or 0) - _fine_coins(player)
    if not seen[team1_id] or not seen[team2_id]:
        return None
    return scores[team1_id], scores[team2_id]


def _score_result(match: ScheduleMatch) -> Optional[tuple[int, int]]:
    if match.team1_id is None or match.team2_id is None:
        return None
    scored = _side_scores(match.players, match.team1_id, match.team2_id)
    if scored is None:
        return None
    team1_score, team2_score = scored
    if team1_score > team2_score:
        return 1, team1_score - team2_score
    if team2_score > team1_score:
        return 2, team2_score - team1_score
    return 3, 0


def rank_table(records: dict[int, dict], team_ids: list[int]) -> dict[int, int]:
    rows = []
    for team_id in team_ids:
        record = records.get(team_id) or _empty_record()
        rows.append({"team_id": team_id, **record})

    clusters: dict[tuple[int, int], list[int]] = {}
    for row in rows:
        clusters.setdefault((row["points"], row["net"]), []).append(row["team_id"])

    def sort_key(row: dict) -> tuple:
        cluster = clusters[(row["points"], row["net"])]
        head_to_head = sum(row["wins_vs"].get(other, 0) for other in cluster if other != row["team_id"])
        return (-row["points"], -row["net"], -head_to_head, row["team_id"])

    ordered = sorted(rows, key=sort_key)
    return {row["team_id"]: index + 1 for index, row in enumerate(ordered)}


def apply_league_result(records: dict[int, dict], team1_id: int, team2_id: int, outcome: int, net_points: int) -> None:
    for team_id in (team1_id, team2_id):
        records.setdefault(team_id, _empty_record())
    records[team1_id]["played"] += 1
    records[team2_id]["played"] += 1
    margin = int(net_points or 0)
    if outcome == 1:
        records[team1_id]["points"] += 2
        records[team1_id]["net"] += margin
        records[team2_id]["net"] -= margin
        records[team1_id]["wins_vs"][team2_id] = records[team1_id]["wins_vs"].get(team2_id, 0) + 1
    elif outcome == 2:
        records[team2_id]["points"] += 2
        records[team2_id]["net"] += margin
        records[team1_id]["net"] -= margin
        records[team2_id]["wins_vs"][team1_id] = records[team2_id]["wins_vs"].get(team1_id, 0) + 1
    elif outcome == 3:
        records[team1_id]["points"] += 1
        records[team2_id]["points"] += 1


RECENT_MATCH_COUNT = 5
HEAD_TO_HEAD_WEIGHT = 0.40
PLAYER_FORM_WEIGHT = 0.35
RECENT_FORM_WEIGHT = 0.25
COIN_EDGE_SCALE = 4.0
LOGISTIC_STEEPNESS = 2.4
LIVE_SCORE_SCALE = 3.0
LIVE_SCORE_WEIGHT = 0.60
LIVE_PRIOR_WEIGHT = 0.40


def _clamp_edge(value: float) -> float:
    return max(-1.0, min(1.0, value))


def win_probability(edges: list[tuple[float, float]]) -> ScheduleWinProbability:
    usable = [(edge, weight) for edge, weight in edges if weight > 0]
    if not usable:
        return ScheduleWinProbability(team1_percent=50, team2_percent=50)
    total_weight = sum(weight for _, weight in usable)
    signal = sum(_clamp_edge(edge) * weight for edge, weight in usable) / total_weight
    team1_share = 1.0 / (1.0 + math.exp(-LOGISTIC_STEEPNESS * signal))
    team1_percent = max(1, min(99, int(round(team1_share * 100))))
    return ScheduleWinProbability(team1_percent=team1_percent, team2_percent=100 - team1_percent)


def _result_for_team(match: dict, team_id: int) -> tuple[int, int]:
    outcome = int(match.get("Outcome") or 3)
    margin = int(match.get("NetPoints") or 0)
    if outcome == 3:
        return 1, 0
    won = (outcome == 1 and int(match["Team1"]) == team_id) or (outcome == 2 and int(match["Team2"]) == team_id)
    if won:
        return 2, margin
    return 0, -margin


def recent_form_edge(team1_matches: list[dict], team2_matches: list[dict], team1_id: int, team2_id: int) -> Optional[float]:
    if not team1_matches or not team2_matches:
        return None

    def rates(matches: list[dict], team_id: int) -> tuple[float, float]:
        points = nets = 0
        for match in matches:
            earned, net = _result_for_team(match, team_id)
            points += earned
            nets += net
        count = len(matches)
        return points / count, nets / count

    points1, net1 = rates(team1_matches, team1_id)
    points2, net2 = rates(team2_matches, team2_id)
    points_edge = _clamp_edge((points1 - points2) / 2.0)
    net_edge = _clamp_edge((net1 - net2) / COIN_EDGE_SCALE)
    return (0.6 * points_edge) + (0.4 * net_edge)


def _before(match: dict, fixture: dict) -> bool:
    if int(match["SeasonId"]) < int(fixture["SeasonId"]):
        return True
    if int(match["SeasonId"]) > int(fixture["SeasonId"]):
        return False
    if int(match["Id"]) == int(fixture["Id"]):
        return False
    return (int(match["Order"]), int(match["Id"])) < (int(fixture["Order"]), int(fixture["Id"]))


def _board_lines(
    lines: list[dict],
    lineup: list[dict],
    team1_id: Optional[int],
    team2_id: Optional[int],
) -> list[dict]:
    """Keep recorded lines and add the rest of the two lineups at zero."""
    sides = {team_id for team_id in (team1_id, team2_id) if team_id is not None}
    by_player = {int(line["PlayerId"]): line for line in lines}
    ordered: list[dict] = []
    for person in lineup:
        team_id = int(person["TeamId"])
        if team_id not in sides:
            continue
        existing = by_player.get(int(person["PlayerId"]))
        if existing is not None:
            ordered.append(existing)
            continue
        ordered.append(
            {
                "PlayerId": person["PlayerId"],
                "FirstName": person["FirstName"],
                "LastName": person["LastName"],
                "AvatarUrl": person.get("AvatarUrl"),
                "RosterTeamId": team_id,
                "CoinsPocketed": 0,
                "OpponentCoinsPocketed": 0,
                "SingleFines": 0,
                "DoubleFines": 0,
                "CoinsFined": 0,
                "StrikersPocketed": 0,
            }
        )
    return ordered


def _same_pair(match: dict, team1_id: int, team2_id: int) -> bool:
    sides = {match.get("Team1"), match.get("Team2")}
    return team1_id in sides and team2_id in sides


class ScheduleRepository:

    def __init__(self, db):
        self.db = db

    def get_schedule(self, season_id: int) -> SeasonScheduleResponse:
        fixtures = self._fixtures(season_id)
        history = self._played_matches(season_id)
        stat_lines = self._stat_lines(season_id)
        roster = self._roster(season_id)
        lineup = self._lineup(season_id)
        team_ids = self._season_team_ids(season_id)
        stats_by_match: dict[int, list[dict]] = {}
        for line in stat_lines:
            stats_by_match.setdefault(int(line["MatchId"]), []).append(line)

        matches = [
            self._build_match(fixture, history, stats_by_match, roster, lineup, team_ids)
            for fixture in fixtures
        ]
        self._apply_rank_changes(matches, fixtures, team_ids)
        return SeasonScheduleResponse(season_id=season_id, matches=matches)

    def _fixtures(self, season_id: int) -> list[dict]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT
                m.Id, m.SeasonId, m.[Order], m.Category, m.Status, m.ScheduledDate,
                m.Team1, m.Team2, t1.Name AS Team1Name, t2.Name AS Team2Name,
                m.Outcome, m.NetPoints, m.GoldenStrike, m.TossOutcome, m.StartedAt, m.ExtraStartedAt,
                m.Duration, m.Extra
            FROM tblMatches m
            LEFT JOIN tblTeams t1 ON t1.Id = m.Team1 AND t1.Void = 0
            LEFT JOIN tblTeams t2 ON t2.Id = m.Team2 AND t2.Void = 0
            WHERE m.SeasonId = %s AND m.Void = 0
            ORDER BY m.[Order] ASC, m.Id ASC
            """,
            (season_id,),
        )
        return cursor.fetchall()

    def _played_matches(self, season_id: int) -> list[dict]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT Id, SeasonId, [Order], Team1, Team2, Outcome, Category, Status, NetPoints
            FROM tblMatches
            WHERE Void = 0
              AND Status = %s
              AND SeasonId <= %s
              AND Team1 IS NOT NULL
              AND Team2 IS NOT NULL
            """,
            (MatchStatus.Played.value, season_id),
        )
        return cursor.fetchall()

    def _stat_lines(self, season_id: int) -> list[dict]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT
                m.Id AS MatchId,
                m.SeasonId,
                m.[Order],
                ms.PlayerId,
                p.FirstName,
                p.LastName,
                p.AvatarUrl,
                pst.TeamId AS RosterTeamId,
                ISNULL(ms.CoinsPocketed, 0) AS CoinsPocketed,
                ISNULL(ms.OpponentCoinsPocketed, 0) AS OpponentCoinsPocketed,
                ISNULL(ms.SingleFines, 0) AS SingleFines,
                ISNULL(ms.DoubleFines, 0) AS DoubleFines,
                ISNULL(ms.CoinsFined, 0) AS CoinsFined,
                ISNULL(ms.StrikersPocketed, 0) AS StrikersPocketed
            FROM tblMatchStats ms
            INNER JOIN tblMatches m
                ON m.Id = ms.MatchId
               AND m.Void = 0
               AND m.Status IN (%s, %s)
               AND m.SeasonId <= %s
            INNER JOIN tblPlayers p ON p.Id = ms.PlayerId AND p.Void = 0
            LEFT JOIN tblPlayersSeasonsTeams pst
                ON pst.PlayerId = ms.PlayerId
               AND pst.SeasonId = m.SeasonId
               AND pst.Void = 0
            WHERE ms.Void = 0
            """,
            (MatchStatus.InProgress.value, MatchStatus.Played.value, season_id),
        )
        return cursor.fetchall()

    def _lineup(self, season_id: int) -> list[dict]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT pst.TeamId, pst.PlayerId, p.FirstName, p.LastName, p.AvatarUrl
            FROM tblPlayersSeasonsTeams pst
            INNER JOIN tblPlayers p ON p.Id = pst.PlayerId AND p.Void = 0
            WHERE pst.SeasonId = %s AND pst.Void = 0
            ORDER BY pst.TeamId, p.FirstName, p.LastName
            """,
            (season_id,),
        )
        return cursor.fetchall()

    def _roster(self, season_id: int) -> dict[int, list[int]]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT TeamId, PlayerId
            FROM tblPlayersSeasonsTeams
            WHERE SeasonId = %s AND Void = 0
            """,
            (season_id,),
        )
        roster: dict[int, list[int]] = {}
        for row in cursor.fetchall():
            roster.setdefault(int(row["TeamId"]), []).append(int(row["PlayerId"]))
        return roster

    def _season_team_ids(self, season_id: int) -> list[int]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT TeamId
            FROM tblSeasonsTeams
            WHERE SeasonId = %s AND Void = 0
            ORDER BY TeamId
            """,
            (season_id,),
        )
        return [int(row["TeamId"]) for row in cursor.fetchall()]

    def _build_match(
        self,
        fixture: dict,
        history: list[dict],
        stats_by_match: dict[int, list[dict]],
        roster: dict[int, list[int]],
        lineup: list[dict],
        team_ids: list[int],
    ) -> ScheduleMatch:
        team1_id = int(fixture["Team1"]) if fixture.get("Team1") is not None else None
        team2_id = int(fixture["Team2"]) if fixture.get("Team2") is not None else None
        status = int(fixture["Status"])
        outcome = int(fixture["Outcome"]) if fixture.get("Outcome") is not None else None
        played = status == MatchStatus.Played.value
        on_board = status in (MatchStatus.InProgress.value, MatchStatus.Played.value)
        lines = stats_by_match.get(int(fixture["Id"]), []) if on_board else []
        if status == MatchStatus.InProgress.value:
            lines = _board_lines(lines, lineup, team1_id, team2_id)
        players = [
            SchedulePlayerStat(
                player_id=int(line["PlayerId"]),
                first_name=line["FirstName"],
                last_name=line["LastName"],
                avatar_url=line.get("AvatarUrl") or None,
                team_id=int(line["RosterTeamId"]) if line.get("RosterTeamId") is not None else None,
                coins_pocketed=int(line["CoinsPocketed"] or 0),
                opponent_coins_pocketed=int(line.get("OpponentCoinsPocketed") or 0),
                single_fines=int(line.get("SingleFines") or 0),
                double_fines=int(line.get("DoubleFines") or 0),
                coins_fined=int(line["CoinsFined"] or 0),
                strikers_pocketed=int(line["StrikersPocketed"] or 0),
            )
            for line in lines
        ]
        head_to_head = self._head_to_head(
            fixture, history, stats_by_match, team1_id, team2_id, include_self=played,
        )
        distribution = self._distribution(players, team1_id, team2_id) if on_board and players else None
        probability = None
        if team1_id is not None and team2_id is not None:
            if status == MatchStatus.Scheduled.value:
                probability = self._probability(fixture, history, stats_by_match, roster, team1_id, team2_id)
            elif status == MatchStatus.InProgress.value:
                probability = self._live_probability(
                    fixture, history, stats_by_match, roster, team1_id, team2_id, players,
                )

        winner_team_id = None
        if played and team1_id is not None and team2_id is not None:
            if outcome == 1:
                winner_team_id = team1_id
            elif outcome == 2:
                winner_team_id = team2_id

        return ScheduleMatch(
            id=int(fixture["Id"]),
            order=int(fixture["Order"]),
            category=int(fixture["Category"]),
            status=status,
            scheduled_date=fixture["ScheduledDate"],
            team1_id=team1_id,
            team2_id=team2_id,
            team1_name=fixture.get("Team1Name"),
            team2_name=fixture.get("Team2Name"),
            outcome=outcome,
            net_points=int(fixture["NetPoints"]) if fixture.get("NetPoints") is not None else None,
            golden_strike=_as_bool(fixture.get("GoldenStrike")),
            winner_team_id=winner_team_id,
            toss_outcome=int(fixture["TossOutcome"]) if fixture.get("TossOutcome") is not None else None,
            started_at=_as_utc_iso(fixture.get("StartedAt")),
            extra_started_at=_as_utc_iso(fixture.get("ExtraStartedAt")),
            duration=int(fixture["Duration"]) if fixture.get("Duration") is not None else None,
            extra=int(fixture["Extra"]) if fixture.get("Extra") is not None else None,
            players=players,
            head_to_head=head_to_head,
            score_distribution=distribution,
            rank_changes=[],
            win_probability=probability,
        )

    def _head_to_head(
        self,
        fixture: dict,
        history: list[dict],
        stats_by_match: dict[int, list[dict]],
        team1_id: Optional[int],
        team2_id: Optional[int],
        include_self: bool,
    ) -> ScheduleHeadToHead:
        empty = ScheduleHeadToHead(
            matches_played=0, team1_wins=0, team2_wins=0, draws=0, team1_coins=0, team2_coins=0,
        )
        if team1_id is None or team2_id is None:
            return empty

        team1_wins = team2_wins = draws = team1_coins = team2_coins = 0
        for match in history:
            if not _same_pair(match, team1_id, team2_id):
                continue
            is_this_match = int(match["Id"]) == int(fixture["Id"])
            if is_this_match and not include_self:
                continue
            if not is_this_match and not _before(match, fixture):
                continue
            outcome = int(match.get("Outcome") or 3)
            match_team1 = int(match["Team1"])
            if outcome == 3:
                draws += 1
            elif (outcome == 1 and match_team1 == team1_id) or (outcome == 2 and match_team1 == team2_id):
                team1_wins += 1
            else:
                team2_wins += 1
            for line in stats_by_match.get(int(match["Id"]), []):
                roster_team = line.get("RosterTeamId")
                coins = int(line.get("CoinsPocketed") or 0)
                if roster_team is not None and int(roster_team) == team1_id:
                    team1_coins += coins
                elif roster_team is not None and int(roster_team) == team2_id:
                    team2_coins += coins
        return ScheduleHeadToHead(
            matches_played=team1_wins + team2_wins + draws,
            team1_wins=team1_wins,
            team2_wins=team2_wins,
            draws=draws,
            team1_coins=team1_coins,
            team2_coins=team2_coins,
        )

    def _distribution(
        self,
        players: list[SchedulePlayerStat],
        team1_id: Optional[int],
        team2_id: Optional[int],
    ) -> ScheduleScoreDistribution:
        totals = {
            team1_id: {"coins": 0, "fines": 0, "strikers": 0},
            team2_id: {"coins": 0, "fines": 0, "strikers": 0},
        }
        for player in players:
            bucket = totals.get(player.team_id)
            if bucket is None:
                continue
            bucket["coins"] += player.coins_pocketed
            bucket["fines"] += player.coins_fined
            bucket["strikers"] += player.strikers_pocketed
        team1 = totals.get(team1_id) or {"coins": 0, "fines": 0, "strikers": 0}
        team2 = totals.get(team2_id) or {"coins": 0, "fines": 0, "strikers": 0}
        total_coins = team1["coins"] + team2["coins"]
        team1_percent = round(team1["coins"] * 100 / total_coins) if total_coins else 50
        return ScheduleScoreDistribution(
            team1_coins=team1["coins"],
            team2_coins=team2["coins"],
            team1_fines=team1["fines"],
            team2_fines=team2["fines"],
            team1_strikers=team1["strikers"],
            team2_strikers=team2["strikers"],
            team1_coin_percent=team1_percent,
        )

    def _live_probability(
        self,
        fixture: dict,
        history: list[dict],
        stats_by_match: dict[int, list[dict]],
        roster: dict[int, list[int]],
        team1_id: int,
        team2_id: int,
        players: list[SchedulePlayerStat],
    ) -> ScheduleWinProbability:
        edges = self._prior_edges(fixture, history, stats_by_match, roster, team1_id, team2_id)
        scored = _side_scores(players, team1_id, team2_id)
        if scored is not None:
            margin = (scored[0] - scored[1]) / LIVE_SCORE_SCALE
            if edges:
                prior_total = sum(weight for _, weight in edges)
                edges = [(edge, weight / prior_total * LIVE_PRIOR_WEIGHT) for edge, weight in edges]
            edges.append((_clamp_edge(margin), LIVE_SCORE_WEIGHT if edges else 1.0))
        return win_probability(edges)

    def _probability(
        self,
        fixture: dict,
        history: list[dict],
        stats_by_match: dict[int, list[dict]],
        roster: dict[int, list[int]],
        team1_id: int,
        team2_id: int,
    ) -> ScheduleWinProbability:
        return win_probability(self._prior_edges(fixture, history, stats_by_match, roster, team1_id, team2_id))

    def _prior_edges(
        self,
        fixture: dict,
        history: list[dict],
        stats_by_match: dict[int, list[dict]],
        roster: dict[int, list[int]],
        team1_id: int,
        team2_id: int,
    ) -> list[tuple[float, float]]:
        player_form: dict[int, dict] = {}
        for match in history:
            if not _before(match, fixture):
                continue
            for line in stats_by_match.get(int(match["Id"]), []):
                player_id = int(line["PlayerId"])
                form = player_form.setdefault(player_id, {"coins": 0, "fines": 0, "strikers": 0, "matches": set()})
                form["coins"] += int(line.get("CoinsPocketed") or 0)
                form["fines"] += int(line.get("CoinsFined") or 0)
                form["strikers"] += int(line.get("StrikersPocketed") or 0)
                form["matches"].add(int(match["Id"]))

        def team_strength(team_id: int) -> float:
            strengths = []
            for player_id in roster.get(team_id, []):
                form = player_form.get(player_id)
                if not form or not form["matches"]:
                    continue
                matches_played = len(form["matches"])
                strengths.append((form["coins"] - form["fines"] - form["strikers"]) / matches_played)
            if not strengths:
                return 0.0
            return sum(strengths) / len(strengths)

        prior = self._head_to_head(fixture, history, stats_by_match, team1_id, team2_id, include_self=False)
        edges: list[tuple[float, float]] = []
        if prior.matches_played:
            head_to_head_edge = (prior.team1_wins - prior.team2_wins) / prior.matches_played
            edges.append((head_to_head_edge, HEAD_TO_HEAD_WEIGHT))

        strength1 = team_strength(team1_id)
        strength2 = team_strength(team2_id)
        if strength1 != 0 or strength2 != 0:
            edges.append((_clamp_edge((strength1 - strength2) / COIN_EDGE_SCALE), PLAYER_FORM_WEIGHT))

        recent_edge = recent_form_edge(
            self._recent_matches(history, fixture, team1_id),
            self._recent_matches(history, fixture, team2_id),
            team1_id,
            team2_id,
        )
        if recent_edge is not None:
            edges.append((recent_edge, RECENT_FORM_WEIGHT))
        return edges

    def _recent_matches(self, history: list[dict], fixture: dict, team_id: int) -> list[dict]:
        played = [
            match for match in history
            if int(match["SeasonId"]) == int(fixture["SeasonId"])
            and _before(match, fixture)
            and team_id in (int(match["Team1"]), int(match["Team2"]))
        ]
        played.sort(key=lambda match: (int(match["Order"]), int(match["Id"])), reverse=True)
        return played[:RECENT_MATCH_COUNT]

    def _apply_rank_changes(self, matches: list[ScheduleMatch], fixtures: list[dict], team_ids: list[int]) -> None:
        records: dict[int, dict] = {team_id: _empty_record() for team_id in team_ids}
        known_teams = list(team_ids)
        names: dict[int, str] = {}
        for fixture in fixtures:
            for side in ("Team1", "Team2"):
                if fixture.get(side) is None:
                    continue
                team_id = int(fixture[side])
                if team_id not in records:
                    records[team_id] = _empty_record()
                    known_teams.append(team_id)
                if fixture.get(f"{side}Name"):
                    names[team_id] = fixture[f"{side}Name"]
        for fixture, match in zip(fixtures, matches):
            status = int(fixture["Status"])
            if int(fixture["Category"]) != MatchCategory.League.value or status not in (
                MatchStatus.Played.value,
                MatchStatus.InProgress.value,
            ):
                continue
            if fixture.get("Team1") is None or fixture.get("Team2") is None:
                continue
            team1_id = int(fixture["Team1"])
            team2_id = int(fixture["Team2"])
            for team_id in (team1_id, team2_id):
                if team_id not in records:
                    records[team_id] = _empty_record()
                    known_teams.append(team_id)
            scored = _score_result(match)
            if scored is not None:
                outcome, net_points = scored
            else:
                outcome = int(fixture.get("Outcome") or 3)
                net_points = int(fixture.get("NetPoints") or 0)
            before = rank_table(records, known_teams)
            if status == MatchStatus.InProgress.value:
                projected = copy.deepcopy(records)
                apply_league_result(projected, team1_id, team2_id, outcome, net_points)
                standing = projected
            else:
                apply_league_result(records, team1_id, team2_id, outcome, net_points)
                standing = records
            after = rank_table(standing, known_teams)
            played = {team1_id, team2_id}
            snapshot = [
                ScheduleRankChange(
                    team_id=team_id,
                    team_name=names.get(team_id),
                    previous_rank=before[team_id],
                    rank=after[team_id],
                    change=before[team_id] - after[team_id],
                    points=standing[team_id]["points"],
                    net=standing[team_id]["net"],
                    in_match=team_id in played,
                )
                for team_id in known_teams
            ]
            snapshot.sort(key=lambda row: row.rank)
            match.rank_changes = snapshot

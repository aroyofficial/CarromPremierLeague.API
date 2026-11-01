from datetime import date
from typing import List, Optional

from pydantic import BaseModel


class SchedulePlayerStat(BaseModel):
    player_id: int
    first_name: str
    last_name: str
    avatar_url: Optional[str] = None
    team_id: Optional[int] = None
    coins_pocketed: int
    opponent_coins_pocketed: int = 0
    single_fines: int = 0
    double_fines: int = 0
    coins_fined: int
    strikers_pocketed: int


class ScheduleHeadToHead(BaseModel):
    matches_played: int
    team1_wins: int
    team2_wins: int
    draws: int
    team1_coins: int
    team2_coins: int


class ScheduleScoreDistribution(BaseModel):
    team1_coins: int
    team2_coins: int
    team1_fines: int
    team2_fines: int
    team1_strikers: int
    team2_strikers: int
    team1_coin_percent: int


class ScheduleRankChange(BaseModel):
    team_id: int
    team_name: Optional[str] = None
    previous_rank: Optional[int] = None
    rank: int
    change: int
    points: int = 0
    net: int = 0
    in_match: bool = False


class ScheduleWinProbability(BaseModel):
    team1_percent: int
    team2_percent: int


class ScheduleMatch(BaseModel):
    id: int
    order: int
    category: int
    status: int
    scheduled_date: date
    team1_id: Optional[int] = None
    team2_id: Optional[int] = None
    team1_name: Optional[str] = None
    team2_name: Optional[str] = None
    outcome: Optional[int] = None
    net_points: Optional[int] = None
    golden_strike: bool
    winner_team_id: Optional[int] = None
    toss_outcome: Optional[int] = None
    started_at: Optional[str] = None
    extra_started_at: Optional[str] = None
    duration: Optional[int] = None
    extra: Optional[int] = None
    players: List[SchedulePlayerStat]
    head_to_head: ScheduleHeadToHead
    score_distribution: Optional[ScheduleScoreDistribution] = None
    rank_changes: List[ScheduleRankChange]
    win_probability: Optional[ScheduleWinProbability] = None


class SeasonScheduleResponse(BaseModel):
    season_id: int
    matches: List[ScheduleMatch]

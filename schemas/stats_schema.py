from pydantic import BaseModel
from typing import Optional, List


class HeadToHeadResponse(BaseModel):
    team1_id: int
    team2_id: int
    matches_played: int
    team1_wins: int
    team2_wins: int
    team1_net_points: int
    team2_net_points: int


class TopCoinPotterResponse(BaseModel):
    player_id: int
    first_name: str
    last_name: str
    avatar_url: Optional[str] = None
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    coins_pocketed: int
    opponent_coins_pocketed: int = 0
    single_fines: int = 0
    double_fines: int = 0
    net_coins: int = 0
    coins_fined: int
    strikers_pocketed: int


class SeasonTopCoinPottersResponse(BaseModel):
    is_league_stage_completed: bool
    players: List[TopCoinPotterResponse]


class PlayerSeasonStatResponse(BaseModel):
    player_id: int
    first_name: str
    last_name: str
    avatar_url: Optional[str] = None
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    matches_played: int
    coins_pocketed: int
    opponent_coins_pocketed: int = 0
    strikers_pocketed: int
    single_fines: int = 0
    double_fines: int = 0
    coins_fined: int = 0
    net_coins: int = 0


class SeasonPlayerStatsResponse(BaseModel):
    data_available: bool
    players: List[PlayerSeasonStatResponse]

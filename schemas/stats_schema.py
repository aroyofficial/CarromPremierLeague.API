from pydantic import BaseModel
from typing import Optional


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

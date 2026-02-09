from pydantic import BaseModel


class HeadToHeadResponse(BaseModel):
    team1_id: int
    team2_id: int
    matches_played: int
    team1_wins: int
    team2_wins: int
    team1_net_points: int
    team2_net_points: int

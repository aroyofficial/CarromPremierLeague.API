from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date


class SeasonCreateRequest(BaseModel):
    name: str = Field(..., min_length=3, max_length=255)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    show: bool = True


class SeasonUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=3, max_length=255)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    show: Optional[bool] = None


class SeasonResponse(BaseModel):
    id: int
    name: str
    start_date: Optional[date]
    end_date: Optional[date]
    show: bool = True

    class Config:
        from_attributes = True


class SeasonTeamAddRequest(BaseModel):
    team_id: int = Field(..., gt=0)


class SeasonTeamResponse(BaseModel):
    team_id: int
    team_name: str
    logo_url: Optional[str] = None


class SeasonListResponse(BaseModel):
    seasons: List[SeasonResponse]

class LeagueTableStanding(BaseModel):
    team_id: int
    team_name: str
    matches_played: int
    wins: int
    draws: int = 0
    points: int
    net_points: int
    head_to_head_wins: int
    golden_strike_wins: int = 0

class LeagueTableResponse(BaseModel):
    winner_id: Optional[int] = None
    season_status: int
    data_available: bool = True
    standings: List[LeagueTableStanding]

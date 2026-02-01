from pydantic import BaseModel, Field, HttpUrl
from typing import Optional, List
from datetime import date


class SeasonCreateRequest(BaseModel):
    name: str = Field(..., min_length=3, max_length=255)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    logo_url: Optional[HttpUrl] = None


class SeasonUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=3, max_length=255)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    logo_url: Optional[HttpUrl] = None


class SeasonResponse(BaseModel):
    id: int
    name: str
    start_date: Optional[date]
    end_date: Optional[date]
    logo_url: Optional[str]

    class Config:
        from_attributes = True


class SeasonListResponse(BaseModel):
    seasons: List[SeasonResponse]

class LeagueTableStanding(BaseModel):
    team_id: int
    team_name: str
    matches_played: int
    wins: int
    points: int
    net_points: int
    head_to_head_wins: int

class LeagueTableResponse(BaseModel):
    standings: List[LeagueTableStanding]

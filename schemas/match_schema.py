from pydantic import BaseModel, Field, model_validator
from typing import Optional
from datetime import date


class MatchCreateRequest(BaseModel):
    team1: int = Field(..., gt=0, description="Team 1 ID")
    team2: int = Field(..., gt=0, description="Team 2 ID")

    scheduled_date: date = Field(
        ...,
        description="Date on which the match is scheduled"
    )

    duration: Optional[int] = Field(
        None,
        ge=0,
        description="Match duration in seconds (nullable until match completes)"
    )

    extra: Optional[int] = Field(
        None,
        ge=0,
        description="Extra time duration in seconds if applicable"
    )

    golden_strike: Optional[bool] = Field(
        False,
        description="Indicates if match was decided by golden strike"
    )

    category: int = Field(
        ...,
        ge=1,
        le=2,
        description="Match category: 1 = League Stage, 2 = Final"
    )

    status: Optional[int] = Field(
        1,
        ge=1,
        le=3,
        description="Match status: 1 = Not Started, 2 = In Progress, 3 = Completed"
    )

    order: Optional[int] = Field(
        None,
        ge=0,
        description="Order of the match within the season schedule"
    )

    season_id: int = Field(
        ...,
        gt=0,
        description="Season ID to which this match belongs"
    )

    net_points: Optional[int] = Field(
        None,
        description="Net points earned in the match (calculated after completion)"
    )

    outcome: Optional[int] = Field(
        None,
        ge=1,
        le=2,
        description="Match outcome: 1 = Team1 won, 2 = Team2 won"
    )

    @model_validator(mode="after")
    def validate_teams(self):
        if self.team1 == self.team2:
            raise ValueError("team1 and team2 cannot be the same")
        return self

class MatchUpdateRequest(BaseModel):
    scheduled_date: Optional[date] = None
    duration: Optional[int] = Field(default=None, ge=0)
    extra: Optional[int] = Field(default=None, ge=0)
    golden_strike: Optional[bool] = None
    status: Optional[int] = Field(default=None, ge=1)
    net_points: Optional[int] = None
    outcome: Optional[int] = Field(default=None, ge=1, le=2)


class MatchResponse(BaseModel):
    id: int
    team1: int
    team2: int
    scheduled_date: date
    duration: Optional[int]
    extra: Optional[int]
    golden_strike: bool
    category: int
    status: int
    season_id: int
    net_points: Optional[int]
    outcome: Optional[int]
from pydantic import BaseModel, Field, model_validator
from typing import Optional, List
from datetime import date

from enums.match_category import MatchCategory
from enums.match_outcome import MatchOutcome
from enums.match_status import MatchStatus


class MatchCreateRequest(BaseModel):
    team1: Optional[int] = None
    team2: Optional[int] = None
    scheduled_date: date
    duration: Optional[int] = None
    extra: Optional[int] = None
    golden_strike: Optional[bool] = None
    category: MatchCategory
    status: MatchStatus
    order: Optional[int]
    season_id: int
    net_points: Optional[int] = None
    outcome: MatchOutcome

    @model_validator(mode="after")
    def validate_teams(self):
        if self.team1 is None and self.team2 is None and self.category == MatchCategory.Final:
            return self
        if self.team1 == self.team2:
            raise ValueError("team1 and team2 cannot be the same")
        return self

class MatchUpdateRequest(BaseModel):
    team1: Optional[int] = None
    team2: Optional[int] = None
    scheduled_date: Optional[date] = None
    duration: Optional[int] = Field(default=None, ge=0)
    extra: Optional[int] = Field(default=None, ge=0)
    golden_strike: Optional[bool] = None
    status: Optional[int] = Field(default=None, ge=1, le=3)
    net_points: Optional[int] = Field(default=None, ge=0, le=255)
    outcome: Optional[int] = Field(default=None, ge=1, le=3)
    toss_outcome: Optional[int] = Field(default=None, ge=1, le=3)


class MatchResponse(BaseModel):
    id: int
    team1: Optional[int] = None
    team2: Optional[int] = None
    scheduled_date: date
    duration: Optional[int]
    extra: Optional[int]
    golden_strike: bool
    category: int
    status: int
    season_id: int
    net_points: Optional[int]
    outcome: Optional[int]
    order: int
    toss_outcome: int

class MatchOrderResponse(BaseModel):
    order: int


class MatchStatUpsertItem(BaseModel):
    player_id: int = Field(gt=0)
    coins_pocketed: int = Field(default=0, ge=0, le=255)
    strikers_pocketed: int = Field(default=0, ge=0, le=255)
    coins_fined: int = Field(default=0, ge=0, le=255)
    shots_taken: int = Field(default=0, ge=0, le=255)


class MatchStatsUpsertRequest(BaseModel):
    stats: List[MatchStatUpsertItem] = Field(min_length=1)


class MatchStatResponse(BaseModel):
    match_id: int
    player_id: int
    coins_pocketed: Optional[int]
    strikers_pocketed: Optional[int]
    coins_fined: Optional[int]
    shots_taken: Optional[int]

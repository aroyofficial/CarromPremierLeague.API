from pydantic import BaseModel, Field


class CompetitionSettings(BaseModel):
    regulation_minutes: int = Field(ge=5, le=30)
    extra_minutes: int = Field(ge=1, le=10)
    league_queen: bool
    queen_needs_cover: bool

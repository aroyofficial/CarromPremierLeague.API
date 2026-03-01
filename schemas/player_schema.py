from pydantic import BaseModel, HttpUrl
from typing import Optional
from datetime import date


class PlayerCreateRequest(BaseModel):
    first_name: str
    last_name: str
    date_of_birth: Optional[date] = None
    avatar_url: Optional[HttpUrl] = None
    nationality_id: Optional[int] = None


class PlayerUpdateRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    avatar_url: Optional[HttpUrl] = None
    nationality_id: Optional[int] = None


class PlayerResponse(BaseModel):
    id: int
    first_name: str
    last_name: str
    date_of_birth: Optional[date]
    avatar_url: Optional[str]
    nationality_id: Optional[int]


class PlayerProfileResponse(PlayerResponse):
    debut_season_id: Optional[int] = None
    debut_season_name: Optional[str] = None
    total_coins_pocketed: int = 0
    total_coins_fined: int = 0
    total_strikers_pocketed: int = 0

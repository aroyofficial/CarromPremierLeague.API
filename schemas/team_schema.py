from pydantic import BaseModel
from typing import Optional

class TeamCreateRequest(BaseModel):
    name: str
    slogan: Optional[str] = None
    logo_url: Optional[str] = None


class TeamUpdateRequest(BaseModel):
    name: Optional[str] = None
    slogan: Optional[str] = None
    logo_url: Optional[str] = None


class TeamResponse(BaseModel):
    id: int
    name: str
    slogan: Optional[str]
    logo_url: Optional[str]

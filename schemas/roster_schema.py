from pydantic import BaseModel
from typing import List
from datetime import datetime


class RosterAssignRequest(BaseModel):
    season_id: int
    team_id: int
    player_id: int


class RosterResponse(BaseModel):
    id: int
    season_id: int
    team_id: int
    player_id: int



class RosterResponse(BaseModel):
    id: int
    player_id: int
    season_id: int
    team_id: int


class RemovePlayerRequest(BaseModel):
    season_id: int
    player_id: int


class TeamRosterPlayer(BaseModel):
    player_id: int
    first_name: str
    last_name: str
    avatar_url: str | None


class TeamRosterResponse(BaseModel):
    team_id: int
    season_id: int
    players: List[TeamRosterPlayer]


class PlayerSeasonHistoryItem(BaseModel):
    season_id: int
    season_name: str
    team_id: int
    team_name: str


class PlayerSeasonHistoryResponse(BaseModel):
    player_id: int
    seasons: List[PlayerSeasonHistoryItem]


class SeasonRosterItem(BaseModel):
    team_id: int
    team_name: str
    player_id: int
    first_name: str
    last_name: str


class SeasonRosterResponse(BaseModel):
    season_id: int
    rosters: List[SeasonRosterItem]

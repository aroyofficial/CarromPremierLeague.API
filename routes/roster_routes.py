from fastapi import APIRouter, Depends

from core.database import get_db
from core.response import ApiResponse
from repositories.roster_repository import RosterRepository
from services.roster_service import RosterService
from controllers.roster_controller import RosterController
from schemas.roster_schema import (
    RosterAssignRequest,
    RosterResponse,
    TeamRosterResponse,
    PlayerSeasonHistoryResponse,
    SeasonRosterResponse,
    RemovePlayerRequest
)


router = APIRouter(
    prefix="/rosters",
    tags=["Roster"]
)


def get_controller(db=Depends(get_db)) -> RosterController:
    repository = RosterRepository(db)
    service = RosterService(repository)
    return RosterController(service)


@router.post(
    "/assign",
    response_model=ApiResponse[RosterResponse]
)
def assign_player(
    request: RosterAssignRequest,
    controller: RosterController = Depends(get_controller)
):
    result = controller.assign_player(
        request.season_id,
        request.team_id,
        request.player_id
    )

    return ApiResponse(
        success=True,
        message="Player assigned successfully",
        data=result
    )

@router.delete("/remove", response_model=ApiResponse[bool])
def remove_player(
    request: RemovePlayerRequest,
    controller=Depends(get_controller)
):
    result = controller.remove_player(
        request.season_id,
        request.player_id
    )

    return ApiResponse(
        success=True,
        message="Player removed successfully",
        data=result
    )

@router.get(
    "/team/{team_id}",
    response_model=ApiResponse[TeamRosterResponse]
)
def get_team_roster(
    team_id: int,
    seasonId: int,
    controller=Depends(get_controller)
):
    result = controller.get_team_roster(seasonId, team_id)

    return ApiResponse(
        success=True,
        message="Team roster fetched successfully",
        data=result
    )

@router.get(
    "/player/{player_id}",
    response_model=ApiResponse[PlayerSeasonHistoryResponse]
)
def get_player_history(
    player_id: int,
    controller=Depends(get_controller)
):
    result = controller.get_player_history(player_id)

    return ApiResponse(
        success=True,
        message="Player season history fetched successfully",
        data=result
    )

@router.get(
    "/season/{season_id}",
    response_model=ApiResponse[SeasonRosterResponse]
)
def get_season_rosters(
    season_id: int,
    controller=Depends(get_controller)
):
    result = controller.get_season_rosters(season_id)

    return ApiResponse(
        success=True,
        message="Season rosters fetched successfully",
        data=result
    )


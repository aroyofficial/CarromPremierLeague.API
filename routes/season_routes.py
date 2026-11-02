from fastapi import APIRouter, Depends
from typing import List

from core.database import get_db
from core.response import ApiResponse
from routes.auth_routes import get_current_admin
from controllers.season_controller import SeasonController
from schemas.schedule_schema import SeasonScheduleResponse
from schemas.season_schema import (
    SeasonCreateRequest,
    SeasonUpdateRequest,
    SeasonResponse,
    LeagueTableResponse,
    SeasonTeamAddRequest,
    SeasonTeamResponse,
)

router = APIRouter(prefix="/seasons", tags=["Seasons"])


@router.post("/", response_model=ApiResponse[SeasonResponse], dependencies=[Depends(get_current_admin)])
def create_season(
    request: SeasonCreateRequest,
    db=Depends(get_db)
):
    controller = SeasonController(db)
    result = controller.create(request)

    return ApiResponse(
        success=True,
        message="Season created successfully",
        data=result
    )


@router.get("/{season_id}", response_model=ApiResponse[SeasonResponse])
def get_season(
    season_id: int,
    db=Depends(get_db)
):
    controller = SeasonController(db)
    result = controller.get_by_id(season_id)

    return ApiResponse(
        success=True,
        message="Season fetched successfully",
        data=result
    )


@router.get("/", response_model=ApiResponse[List[SeasonResponse]])
def get_all_seasons(
    db=Depends(get_db)
):
    controller = SeasonController(db)
    result = controller.get_all()

    return ApiResponse(
        success=True,
        message="Seasons fetched successfully",
        data=result
    )


@router.put("/{season_id}", response_model=ApiResponse[SeasonResponse], dependencies=[Depends(get_current_admin)])
def update_season(
    season_id: int,
    request: SeasonUpdateRequest,
    db=Depends(get_db)
):
    controller = SeasonController(db)
    result = controller.update(season_id, request)

    return ApiResponse(
        success=True,
        message="Season updated successfully",
        data=result
    )


@router.delete("/{season_id}", response_model=ApiResponse[bool], dependencies=[Depends(get_current_admin)])
def delete_season(
    season_id: int,
    db=Depends(get_db)
):
    controller = SeasonController(db)
    result = controller.delete(season_id)

    return ApiResponse(
        success=True,
        message="Season deleted successfully",
        data=result
    )


@router.get("/{season_id}/teams", response_model=ApiResponse[List[SeasonTeamResponse]])
def get_season_teams(season_id: int, db=Depends(get_db)):
    return ApiResponse(
        success=True,
        message="Season teams fetched successfully",
        data=SeasonController(db).get_teams(season_id)
    )


@router.post(
    "/{season_id}/teams",
    response_model=ApiResponse[List[SeasonTeamResponse]],
    dependencies=[Depends(get_current_admin)]
)
def add_season_team(season_id: int, request: SeasonTeamAddRequest, db=Depends(get_db)):
    return ApiResponse(
        success=True,
        message="Team added to season successfully",
        data=SeasonController(db).add_team(season_id, request.team_id)
    )


@router.delete(
    "/{season_id}/teams/{team_id}",
    response_model=ApiResponse[bool],
    dependencies=[Depends(get_current_admin)]
)
def remove_season_team(season_id: int, team_id: int, db=Depends(get_db)):
    return ApiResponse(
        success=True,
        message="Team removed from season successfully",
        data=SeasonController(db).remove_team(season_id, team_id)
    )


@router.get(
    "/{season_id}/schedule",
    response_model=ApiResponse[SeasonScheduleResponse]
)
def get_schedule(
    season_id: int,
    db=Depends(get_db)
):
    controller = SeasonController(db)
    result = controller.get_schedule(season_id)

    return ApiResponse(
        success=True,
        message="Season schedule fetched successfully",
        data=result
    )


@router.get(
    "/{season_id}/league-table",
    response_model=ApiResponse[LeagueTableResponse]
)
def get_league_table(
    season_id: int,
    db=Depends(get_db)
):
    controller = SeasonController(db)
    result = controller.get_league_table(season_id)

    return ApiResponse(
        success=True,
        message="League table fetched successfully",
        data=result
    )

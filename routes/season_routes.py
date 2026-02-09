from fastapi import APIRouter, Depends
from typing import List

from core.database import get_db
from core.response import ApiResponse
from controllers.season_controller import SeasonController
from schemas.season_schema import (
    SeasonCreateRequest,
    SeasonUpdateRequest,
    SeasonResponse,
    LeagueTableResponse
)

router = APIRouter(prefix="/seasons", tags=["Seasons"])


@router.post("/", response_model=ApiResponse[SeasonResponse])
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


@router.put("/{season_id}", response_model=ApiResponse[SeasonResponse])
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


@router.delete("/{season_id}", response_model=ApiResponse[bool])
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

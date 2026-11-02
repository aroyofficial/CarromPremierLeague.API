from fastapi import APIRouter, Depends
from typing import List
from core.database import get_db
from core.response import ApiResponse
from repositories.match_repository import MatchRepository
from services.match_service import MatchService
from controllers.match_controller import MatchController
from routes.auth_routes import get_current_admin
from schemas.match_schema import (
    MatchCreateRequest,
    MatchUpdateRequest,
    MatchResponse,
    MatchOrderResponse,
    MatchStatsUpsertRequest,
    MatchStatResponse
)


router = APIRouter(
    prefix="/matches",
    tags=["Matches"]
)


def get_controller(db=Depends(get_db)) -> MatchController:
    repository = MatchRepository(db)
    service = MatchService(repository)
    return MatchController(service)


@router.post("", response_model=ApiResponse[MatchResponse], dependencies=[Depends(get_current_admin)])
def create(request: MatchCreateRequest, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Match created successfully",
        data=controller.create(request)
    )



@router.get("", response_model=ApiResponse[List[MatchResponse]])
def get_all(seasonId: int = None, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Matches fetched successfully",
        data=controller.get_all(season_id=seasonId)
    )



@router.get("/next-order", response_model=ApiResponse[MatchOrderResponse])
def get_next_match_order(seasonId: int, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Next match order fetched successfully",
        data=controller.get_next_match_order(seasonId)
    )



@router.get("/{match_id}", response_model=ApiResponse[MatchResponse])
def get_by_id(match_id: int, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Match fetched successfully",
        data=controller.get_by_id(match_id)
   )


@router.patch("/{match_id}", response_model=ApiResponse[MatchResponse], dependencies=[Depends(get_current_admin)])
def update(match_id: int, request: MatchUpdateRequest, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Match updated successfully",
        data=controller.update(match_id, request)
    )


@router.post("/{match_id}/reset", response_model=ApiResponse[MatchResponse], dependencies=[Depends(get_current_admin)])
def reset(match_id: int, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Match reset to scheduled",
        data=controller.reset(match_id)
    )


@router.get("/{match_id}/stats", response_model=ApiResponse[List[MatchStatResponse]])
def get_stats(match_id: int, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Match stats fetched successfully",
        data=controller.get_stats(match_id)
    )


@router.put("/{match_id}/stats", response_model=ApiResponse[List[MatchStatResponse]], dependencies=[Depends(get_current_admin)])
def upsert_stats(match_id: int, request: MatchStatsUpsertRequest, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Match stats updated successfully",
        data=controller.upsert_stats(match_id, request)
    )


@router.delete("/{match_id}", response_model=ApiResponse[bool], dependencies=[Depends(get_current_admin)])
def delete(match_id: int, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Match deleted successfully",
        data=controller.delete(match_id)
    )

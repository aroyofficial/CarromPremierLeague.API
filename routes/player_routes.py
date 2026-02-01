from fastapi import APIRouter, Depends
from typing import List

from repositories.player_repository import PlayerRepository
from services.player_service import PlayerService
from controllers.player_controller import PlayerController
from repositories.country_repository import CountryRepository
from services.country_service import CountryService
from schemas.player_schema import (
    PlayerCreateRequest,
    PlayerUpdateRequest,
    PlayerResponse
)
from core.database import get_db
from core.response import ApiResponse


router = APIRouter(
    prefix="/players",
    tags=["Players"]
)


def get_controller(db=Depends(get_db)) -> PlayerController:
    player_repository = PlayerRepository(db)
    country_repository = CountryRepository(db)
    country_service = CountryService(country_repository)
    service = PlayerService(player_repository, country_service)
    return PlayerController(service)


@router.get("/", response_model=ApiResponse[List[PlayerResponse]])
def get_all(controller: PlayerController = Depends(get_controller)):
    result = controller.get_all()

    return ApiResponse(
        success=True,
        message="Players fetched successfully",
        data=result
    )


@router.get("/{player_id}", response_model=ApiResponse[PlayerResponse])
def get_by_id(
    player_id: int,
    controller: PlayerController = Depends(get_controller)
):
    result = controller.get_by_id(player_id)

    return ApiResponse(
        success=True,
        message="Player fetched successfully",
        data=result
    )


@router.post("/", response_model=ApiResponse[PlayerResponse])
def create(
    request: PlayerCreateRequest,
    controller: PlayerController = Depends(get_controller)
):
    result = controller.create(request)

    return ApiResponse(
        success=True,
        message="Player created successfully",
        data=result
    )


@router.patch("/{player_id}", response_model=ApiResponse[PlayerResponse])
def update(
    player_id: int,
    request: PlayerUpdateRequest,
    controller: PlayerController = Depends(get_controller)
):
    result = controller.update(player_id, request)

    return ApiResponse(
        success=True,
        message="Player updated successfully",
        data=result
    )


@router.delete("/{player_id}", response_model=ApiResponse[bool])
def delete(
    player_id: int,
    controller: PlayerController = Depends(get_controller)
):
    result = controller.delete(player_id)

    return ApiResponse(
        success=True,
        message="Player deleted successfully",
        data=result
    )
